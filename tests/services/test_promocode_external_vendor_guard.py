"""Money-safety: promocode day/trial extensions must not free-extend a vendor sub.

Two promocode effects extend an existing subscription: the days grant
(SUBSCRIPTION_DAYS / BALANCE_AND_DAYS) and the trial grant that finds a same-tariff
subscription. For an external-vendor (Artemida) subscription, ``extend_subscription``
moves the local ``end_date`` without renewing the vendor key — the paying-nothing
client silently loses access, and honoring it would charge the owner the vendor fee
for a giveaway. Both are skipped the way this service already signals a non-applied
effect: raise a whitelisted ``ValueError`` so the reserved use is refunded and the
caller returns ``{'success': False, 'error': 'external_vendor_not_extendable'}``. A
remnawave subscription is extended exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.database.models import (
    Base,
    PromoCode,
    PromoCodeType,
    Subscription,
    SubscriptionStatus,
    Tariff,
    User,
    UserStatus,
)
from app.services.promocode_service import PromoCodeService
from tests.fixtures.sqlite_memory import memory_session


# extend_subscription попутно чистит пакеты трафика/уведомления/автоплатёж, а
# resolve тарифа тянет promo-group связи — перечислять таблицы поимённо значит
# ловить «no such table» по одной; берём всю схему целиком, как в referral-тестах.
TABLES = list(Base.metadata.sorted_tables)


@pytest.fixture(autouse=True)
def no_panel_sync(monkeypatch):
    async def noop(self, db, subscription, **kwargs):
        return None

    monkeypatch.setattr('app.services.subscription_service.SubscriptionService.update_remnawave_user', noop)


def _user(db) -> User:
    user = User(
        telegram_id=111,
        username='user111',
        first_name='User',
        status=UserStatus.ACTIVE.value,
        language='ru',
        balance_kopeks=0,
    )
    db.add(user)
    return user


def _sub(user_id: int, *, provider: str | None, tariff_id: int | None) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        user_id=user_id,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        external_provider=provider,
        external_ref='key_1' if provider and provider != 'remnawave' else None,
        tariff_id=tariff_id,
        start_date=now - timedelta(days=5),
        end_date=now + timedelta(days=25),
        remnawave_short_id='short1',
    )


def _days_promocode() -> PromoCode:
    now = datetime.now(UTC)
    return PromoCode(
        code='DAYS30',
        type=PromoCodeType.SUBSCRIPTION_DAYS.value,
        subscription_days=30,
        max_uses=10,
        current_uses=0,
        is_active=True,
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=1),
    )


class TestDaysPromocode:
    @pytest.mark.asyncio
    async def test_artemida_subscription_is_not_extended(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            user = _user(db)
            await db.commit()
            sub = _sub(user.id, provider='artemida', tariff_id=None)
            db.add(sub)
            db.add(_days_promocode())
            await db.commit()
            before = sub.end_date

            result = await PromoCodeService().activate_promocode(db, user.id, 'DAYS30')

            assert result == {'success': False, 'error': 'external_vendor_not_extendable'}
            await db.refresh(sub)
            assert sub.end_date == before, 'вендорную подписку не двигаем'
            # Reserved use slot is refunded (claim increment rolled back) — the code
            # stays retryable, exactly like every other whitelisted skip in this service.
            promo = await db.scalar(select(PromoCode).where(PromoCode.code == 'DAYS30'))
            assert promo.current_uses == 0

    @pytest.mark.asyncio
    async def test_remnawave_subscription_is_extended_as_before(self, monkeypatch):
        async with memory_session(monkeypatch, TABLES) as db:
            user = _user(db)
            await db.commit()
            sub = _sub(user.id, provider=None, tariff_id=None)
            db.add(sub)
            db.add(_days_promocode())
            await db.commit()
            before = sub.end_date

            result = await PromoCodeService().activate_promocode(db, user.id, 'DAYS30')

            assert result['success'] is True
            await db.refresh(sub)
            assert sub.end_date == before + timedelta(days=30)


class TestTrialPromocode:
    @pytest.mark.asyncio
    async def test_artemida_same_tariff_subscription_is_not_extended(self, monkeypatch):
        monkeypatch.setattr(
            'app.database.crud.server_squad.get_effective_tariff_squad_uuids',
            _async_return([]),
        )
        async with memory_session(monkeypatch, TABLES) as db:
            user = _user(db)
            tariff = Tariff(name='Максимум', period_prices={'30': 49900}, allowed_squads=[])
            db.add(tariff)
            await db.commit()
            sub = _sub(user.id, provider='artemida', tariff_id=tariff.id)
            db.add(sub)
            db.add(_trial_promocode(tariff.id))
            await db.commit()
            before = sub.end_date

            result = await PromoCodeService().activate_promocode(db, user.id, 'TRIALTAR')

            assert result == {'success': False, 'error': 'external_vendor_not_extendable'}
            await db.refresh(sub)
            assert sub.end_date == before
            promo = await db.scalar(select(PromoCode).where(PromoCode.code == 'TRIALTAR'))
            assert promo.current_uses == 0

    @pytest.mark.asyncio
    async def test_remnawave_same_tariff_subscription_is_extended_as_before(self, monkeypatch):
        monkeypatch.setattr(
            'app.database.crud.server_squad.get_effective_tariff_squad_uuids',
            _async_return([]),
        )
        async with memory_session(monkeypatch, TABLES) as db:
            user = _user(db)
            tariff = Tariff(name='Максимум', period_prices={'30': 49900}, allowed_squads=[])
            db.add(tariff)
            await db.commit()
            sub = _sub(user.id, provider=None, tariff_id=tariff.id)
            db.add(sub)
            db.add(_trial_promocode(tariff.id))
            await db.commit()
            before = sub.end_date

            result = await PromoCodeService().activate_promocode(db, user.id, 'TRIALTAR')

            assert result['success'] is True
            await db.refresh(sub)
            assert sub.end_date == before + timedelta(days=7)


def _trial_promocode(tariff_id: int) -> PromoCode:
    now = datetime.now(UTC)
    return PromoCode(
        code='TRIALTAR',
        type=PromoCodeType.TRIAL_SUBSCRIPTION.value,
        tariff_id=tariff_id,
        subscription_days=7,
        max_uses=10,
        current_uses=0,
        is_active=True,
        valid_from=now - timedelta(days=1),
        valid_until=now + timedelta(days=1),
    )


def _async_return(value):
    async def _inner(*args, **kwargs):
        return value

    return _inner
