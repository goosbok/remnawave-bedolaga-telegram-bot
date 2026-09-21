"""Client journey e2e — the two trials in sequence, through the real cabinet handler.

The two-trials feature promises a user can take BOTH the limited trial (own
Remnawave nodes) AND the unlimited trial (Artemida vendor), because the gate is
kind-aware (`User.has_used_trial(kind)`) rather than a single "trial used" flag.
This drives the REAL cabinet handler `activate_unlimited_trial` (no HTTP/auth
layer, same style as tests/cabinet/test_unlimited_trial_endpoint.py) with the
vendor faked at the `ArtemidaClient` boundary — no network, no 2 ₽ spent.

The paid buy → renew → rebranded-link → vendor-swap half of the journey is proven
end-to-end (real provider + rebrand route, mocked vendor) in
tests/integration/test_artemida_full_chain.py.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database.models import (
    DiscountOffer,
    LavaSubscription,
    PlategaSubscription,
    PromoGroup,
    Subscription,
    SubscriptionEvent,
    Tariff,
    User,
    tariff_promo_groups,
)
from app.services import unlimited_trial_service
from tests.fixtures.sqlite_memory import memory_session


# Same table set as tests/cabinet/test_unlimited_trial_endpoint.py: the rollback
# path's db.delete(subscription) loads these backrefs during flush.
_TABLES = (
    User.__table__,
    Tariff.__table__,
    PromoGroup.__table__,
    tariff_promo_groups,
    Subscription.__table__,
    DiscountOffer.__table__,
    PlategaSubscription.__table__,
    LavaSubscription.__table__,
    SubscriptionEvent.__table__,
)


class _FakeArtemidaClient:
    """Stand-in for ArtemidaClient: no network, no live vendor key."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def create_key(self, **kwargs):
        return SimpleNamespace(
            id='key_unltrial_1',
            devices=kwargs['devices'],
            subscription_url='https://vendor/x',
            expire_at=None,
        )

    async def create_trial(self, **kwargs):
        # Trials provision via POST /trial (create_key rejects a 1-day period).
        return SimpleNamespace(
            id='key_unltrial_1',
            devices=2,
            subscription_url='https://vendor/x',
            expire_at=None,
        )


def _configure_artemida(monkeypatch) -> None:
    monkeypatch.setattr('app.config.settings.ARTEMIDA_ENABLED', True, raising=False)
    monkeypatch.setattr('app.config.settings.ARTEMIDA_TRIAL_ENABLED', True, raising=False)
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )
    monkeypatch.setattr('app.services.providers.artemida.ArtemidaClient', lambda *a, **k: _FakeArtemidaClient())


async def _make_user(db, **overrides) -> User:
    defaults: dict = dict(email='journey-user@example.com', email_verified=True, auth_type='email')
    defaults.update(overrides)
    user = User(**defaults)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    await db.refresh(user, ['subscriptions'])
    return user


async def _make_tariff(db, **overrides) -> Tariff:
    defaults: dict = dict(
        name='tariff',
        is_trial_available=True,
        is_active=True,
        device_limit=2,
        traffic_limit_gb=0,
        period_prices={'1': 0},
    )
    defaults.update(overrides)
    tariff = Tariff(**defaults)
    db.add(tariff)
    await db.commit()
    await db.refresh(tariff)
    return tariff


def _used_trial(user_id: int, tariff_id: int, *, external_provider: str | None) -> Subscription:
    """An already-used (expired) trial. external_provider decides the KIND:
    None/'remnawave' -> limited; a vendor name -> unlimited."""
    now = datetime.now(UTC)
    return Subscription(
        user_id=user_id,
        tariff_id=tariff_id,
        status='expired',
        is_trial=True,
        external_provider=external_provider,
        external_ref='key_old' if external_provider else None,
        start_date=now - timedelta(days=4),
        end_date=now - timedelta(days=1),
        traffic_limit_gb=0,
        device_limit=2,
        remnawave_short_id='old_journey',
    )


@pytest.mark.asyncio
async def test_limited_trial_holder_can_still_take_unlimited(monkeypatch):
    """A user who already used the LIMITED trial can still activate the UNLIMITED
    one — the kind-aware gate does not conflate the two."""
    from app.cabinet.routes.subscription_modules.purchase import activate_unlimited_trial

    _configure_artemida(monkeypatch)

    async with memory_session(monkeypatch, _TABLES) as db:
        limited_tariff = await _make_tariff(db, name='Триал (свои ноды)', provider='remnawave')
        await _make_tariff(db, name='Безлимит-триал', provider='artemida')
        user = await _make_user(db)

        # The user has already used their limited (own-nodes) trial.
        db.add(_used_trial(user.id, limited_tariff.id, external_provider=None))
        await db.commit()
        await db.refresh(user, ['subscriptions'])

        # Gate: limited is spent, unlimited is still open.
        assert user.has_used_trial('limited') is True
        assert user.has_used_trial('unlimited') is False
        assert unlimited_trial_service.unlimited_trial_available(user) is True

        response = await activate_unlimited_trial(user=user, db=db)
        assert response.is_trial is True

        # A second, vendor-backed trial subscription now exists.
        rows = (await db.execute(select(Subscription).where(Subscription.user_id == user.id))).scalars().all()
        vendor_subs = [s for s in rows if s.external_provider == 'artemida']
        assert len(vendor_subs) == 1
        unlimited = vendor_subs[0]
        assert unlimited.is_trial is True
        assert unlimited.external_ref == 'key_unltrial_1'
        # The client link is OUR rebranded token URL, never the vendor key id.
        assert unlimited.subscription_url.startswith('https://sub.max/a/')
        assert 'key_unltrial_1' not in unlimited.subscription_url

        # Both kinds are now spent; neither can be taken again.
        await db.refresh(user, ['subscriptions'])
        assert user.has_used_trial('limited') is True
        assert user.has_used_trial('unlimited') is True
        assert unlimited_trial_service.unlimited_trial_available(user) is False


@pytest.mark.asyncio
async def test_unlimited_trial_holder_has_not_spent_the_limited_one(monkeypatch):
    """The reverse gate: using the UNLIMITED trial must not consume the LIMITED
    eligibility (they are independent kinds)."""
    _configure_artemida(monkeypatch)

    async with memory_session(monkeypatch, _TABLES) as db:
        limited_tariff = await _make_tariff(db, name='Триал (свои ноды)', provider='remnawave')
        user = await _make_user(db)

        # The user has already used their unlimited (vendor) trial.
        db.add(_used_trial(user.id, limited_tariff.id, external_provider='artemida'))
        await db.commit()
        await db.refresh(user, ['subscriptions'])

        assert user.has_used_trial('unlimited') is True
        assert user.has_used_trial('limited') is False
        # The unlimited trial itself can't be taken twice.
        assert unlimited_trial_service.unlimited_trial_available(user) is False
