"""A vendor (external-provider) trial must never be offered or accepted as a renewal.

An ARTΞMIDA trial is a separate vendor trial key (``POST /trial``). The vendor
refuses ``/keys/{id}/renew`` on it outright ("Пробный ключ нельзя продлевать"), so
a renewal of such a subscription can only ever fail at the vendor — after the
balance was charged and the local ``end_date`` committed, leaving a compensation
to clean up and an error report for the admins.

Worse, the premium trial tariff is priced ``{"30": 0}``, which the free-tariff rule
treats as a deliberately free period: without a guard the cabinet offers the
expired trial a 30-day renewal for 0 ₽. Only the vendor's refusal stopped a free
month of premium.

The rule: a trial served by an external vendor is not renewable. The client must
buy a paid tariff instead (a fresh paid key). An own-panel (remnawave) trial is
unaffected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.database.models import Base, Subscription, SubscriptionStatus, Tariff, User
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


class _FailIfCalled:
    """Any vendor/panel sync reaching here means the renewal was not stopped early."""

    async def renew_external(self, db, subscription, *, period_days):
        raise AssertionError('renew_external must not be reached for a vendor trial')

    async def update_remnawave_user(self, db, subscription, **kwargs):
        raise AssertionError('panel sync must not be reached for a vendor trial')

    async def create_remnawave_user(self, db, subscription, **kwargs):
        raise AssertionError('panel sync must not be reached for a vendor trial')


@pytest.fixture(autouse=True)
def tariffs_mode(monkeypatch):
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)


@pytest.fixture(autouse=True)
def no_sync(monkeypatch):
    import app.services.subscription_renewal_service as renewal_module
    import app.services.subscription_service as subscription_service_module

    monkeypatch.setattr(subscription_service_module, 'SubscriptionService', _FailIfCalled)
    monkeypatch.setattr(renewal_module, 'SubscriptionService', _FailIfCalled)


def _user() -> User:
    return User(id=1, telegram_id=1001, first_name='U', language='ru', status='active', balance_kopeks=500_00)


def _premium_trial_tariff() -> Tariff:
    # Mirrors prod tariff 19: hidden trial tariff priced {"30": 0}.
    return Tariff(
        id=19,
        name='Премиум подписка на 1 день',
        description='',
        is_active=False,
        is_trial_available=True,
        is_daily=False,
        period_prices={'30': 0},
        traffic_limit_gb=0,
        device_limit=1,
        allowed_squads=[],
        display_order=0,
        provider='artemida',
    )


def _vendor_trial(*, expired: bool = True) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=232,
        user_id=1,
        status=(SubscriptionStatus.EXPIRED if expired else SubscriptionStatus.ACTIVE).value,
        is_trial=True,
        start_date=now - timedelta(days=2),
        end_date=now - timedelta(days=1) if expired else now + timedelta(hours=12),
        traffic_limit_gb=0,
        traffic_used_gb=0.0,
        device_limit=1,
        tariff_id=19,
        connected_squads=[],
        external_provider='artemida',
        external_ref='key_trial_1',
    )


async def _seed(db, subscription: Subscription) -> User:
    db.add_all([_user(), _premium_trial_tariff(), subscription])
    await db.commit()
    return await db.get(User, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize('expired', [True, False])
async def test_vendor_trial_gets_no_renewal_options(monkeypatch, expired):
    from app.cabinet.routes.subscription_modules.renewal import get_renewal_options

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed(db, _vendor_trial(expired=expired))
        options = await get_renewal_options(user=user, db=db, subscription_id=232)

    assert options == []


@pytest.mark.asyncio
async def test_vendor_trial_renewal_is_rejected_before_any_charge(monkeypatch):
    from app.cabinet.routes.subscription_modules.renewal import renew_subscription
    from app.cabinet.schemas.subscription import RenewalRequest

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed(db, _vendor_trial())
        end_before = (await db.get(Subscription, 232)).end_date

        with pytest.raises(HTTPException) as exc:
            await renew_subscription(request=RenewalRequest(period_days=30), user=user, db=db, subscription_id=232)

        subscription = await db.get(Subscription, 232)
        user_after = await db.get(User, 1)

    assert exc.value.status_code == 400
    assert exc.value.detail['code'] == 'trial_not_renewable'
    assert subscription.end_date == end_before
    assert subscription.status == SubscriptionStatus.EXPIRED.value
    assert user_after.balance_kopeks == 500_00


def test_predicate_blocks_only_vendor_trials():
    from app.services.subscription_renewal_service import is_non_renewable_vendor_trial

    assert is_non_renewable_vendor_trial(SimpleNamespace(is_trial=True, external_provider='artemida'))
    # Own-panel trial (tariff 8) keeps its paid renewal path.
    assert not is_non_renewable_vendor_trial(SimpleNamespace(is_trial=True, external_provider=None))
    assert not is_non_renewable_vendor_trial(SimpleNamespace(is_trial=True, external_provider='remnawave'))
    # A paid vendor key renews normally.
    assert not is_non_renewable_vendor_trial(SimpleNamespace(is_trial=False, external_provider='artemida'))


@pytest.mark.asyncio
async def test_miniapp_offers_no_renewal_for_vendor_trial(monkeypatch):
    from app.webapi.routes.miniapp import _prepare_subscription_renewal_options

    async with memory_session(monkeypatch, TABLES) as db:
        user = await _seed(db, _vendor_trial())
        subscription = await db.get(Subscription, 232)
        periods, pricing_map, default_period = await _prepare_subscription_renewal_options(db, user, subscription)

    assert periods == []
    assert pricing_map == {}
    assert default_period is None
