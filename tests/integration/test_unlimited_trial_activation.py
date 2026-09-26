"""Real-DB, in-process integration test for the unlimited (Artemida) trial activation flow.

Mirrors ``tests/integration/test_artemida_provision_e2e.py``'s approach (real
``Tariff``/``Subscription``/``User`` rows in an in-memory SQLite session, vendor HTTP
faked at the ``ArtemidaClient`` boundary), but drives the flow through
``activate_unlimited_trial`` end-to-end: eligibility guard, trial-subscription
creation, vendor provisioning, and rollback on vendor failure.

The rollback path is exercised via a single vendor-error subclass
(``ArtemidaInsufficientBalance``) rather than every ``ArtemidaAPIError`` subclass —
the activation code catches broadly (``except Exception``) around the
provisioning call, so which concrete error triggers it doesn't change the
rollback behaviour under test.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

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
from app.external.artemida_api import ArtemidaInsufficientBalance
from app.services.unlimited_trial_service import UnlimitedTrialActivationError, activate_unlimited_trial
from tests.fixtures.sqlite_memory import memory_session


# Mirrors _TABLES in test_artemida_provision_e2e.py, plus:
# - User.__table__: the eligibility guard and post-activation assertions both need
#   a real User row (has_used_trial reads user.subscriptions).
# - DiscountOffer/PlategaSubscription/LavaSubscription/SubscriptionEvent
#   __table__: each has a `subscription = relationship('Subscription',
#   backref=...)` WITHOUT passive_deletes=True, so the rollback path's
#   `db.delete(subscription)` loads all four during flush to null out the FK on
#   any matching rows — missing any of them fails the flush and poisons the
#   session (discovered by running the test and reading the real errors).
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

    def __init__(self, *, fail_with: Exception | None = None):
        self._fail_with = fail_with
        self.kwargs: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def create_key(self, **kwargs):
        if self._fail_with is not None:
            raise self._fail_with
        self.kwargs = kwargs
        return SimpleNamespace(
            id='key_trial_1',
            devices=kwargs['devices'],
            subscription_url='https://vendor/x',
            expire_at=None,
        )

    async def create_trial(self, **kwargs):
        # The trial provisions via POST /trial, not create_key (the vendor rejects
        # a 1-day create_key as out-of-range). Devices are vendor-fixed at 2.
        if self._fail_with is not None:
            raise self._fail_with
        self.kwargs = kwargs
        return SimpleNamespace(
            id='key_trial_1',
            devices=2,
            subscription_url='https://vendor/x',
            expire_at=None,
        )


def _configure_artemida(monkeypatch, *, fail_with: Exception | None = None) -> _FakeArtemidaClient:
    monkeypatch.setattr('app.config.settings.ARTEMIDA_ENABLED', True, raising=False)
    monkeypatch.setattr('app.config.settings.ARTEMIDA_TRIAL_ENABLED', True, raising=False)
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )
    fake_client = _FakeArtemidaClient(fail_with=fail_with)
    monkeypatch.setattr('app.services.providers.artemida.ArtemidaClient', lambda *a, **k: fake_client)
    return fake_client


async def _make_verified_user(db) -> User:
    user = User(email='trial-user@example.com', email_verified=True, auth_type='email')
    db.add(user)
    await db.commit()
    await db.refresh(user)
    # `has_used_trial` reads `user.subscriptions`; a freshly created User bound to
    # an AsyncSession has this lazy relationship unloaded, and accessing it
    # synchronously (as `has_used_trial` does) would raise MissingGreenlet. Load it
    # explicitly now (it's empty at this point) — same precondition documented on
    # `User.is_trial_already_used`.
    await db.refresh(user, ['subscriptions'])
    return user


async def _make_trial_tariff(db) -> Tariff:
    tariff = Tariff(
        name='Безлимит-триал',
        provider='artemida',
        is_trial_available=True,
        is_active=True,
        device_limit=2,
        traffic_limit_gb=0,
        period_prices={'1': 0},
    )
    db.add(tariff)
    await db.commit()
    await db.refresh(tariff)
    return tariff


@pytest.mark.asyncio
async def test_activation_creates_unlimited_trial(monkeypatch):
    fake_client = _configure_artemida(monkeypatch)

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff = await _make_trial_tariff(db)
        user = await _make_verified_user(db)

        subscription = await activate_unlimited_trial(db, user)

        assert subscription.is_trial is True
        assert subscription.external_provider == 'artemida'
        assert subscription.external_ref == 'key_trial_1'
        assert subscription.tariff_id == tariff.id
        assert fake_client.kwargs is not None
        # Trial provisions via create_trial (vendor-fixed devices), so the device
        # limit is taken from the returned key rather than a create_key argument.
        assert subscription.device_limit == tariff.device_limit

        await db.refresh(user, ['subscriptions'])
        assert user.has_used_trial('unlimited') is True
        assert user.has_used_trial('limited') is False


@pytest.mark.asyncio
async def test_activation_rolls_back_on_vendor_failure(monkeypatch):
    _configure_artemida(monkeypatch, fail_with=ArtemidaInsufficientBalance('no funds'))

    async with memory_session(monkeypatch, _TABLES) as db:
        await _make_trial_tariff(db)
        user = await _make_verified_user(db)

        with pytest.raises(ArtemidaInsufficientBalance):
            await activate_unlimited_trial(db, user)

        await db.refresh(user, ['subscriptions'])
        assert user.has_used_trial('unlimited') is False

        remaining = (await db.execute(select(Subscription).where(Subscription.user_id == user.id))).scalars().all()
        assert remaining == []


@pytest.mark.asyncio
async def test_activation_raises_activation_error_when_rollback_also_fails(monkeypatch):
    _configure_artemida(monkeypatch, fail_with=ArtemidaInsufficientBalance('no funds'))
    # Patched where unlimited_trial_service imports it from (its local `from
    # app.services.trial_activation_service import rollback_trial_subscription_activation`
    # re-resolves the name from that module on every call, so patching the
    # attribute there is what actually takes effect).
    monkeypatch.setattr(
        'app.services.trial_activation_service.rollback_trial_subscription_activation',
        AsyncMock(return_value=False),
    )

    async with memory_session(monkeypatch, _TABLES) as db:
        await _make_trial_tariff(db)
        user = await _make_verified_user(db)

        with pytest.raises(UnlimitedTrialActivationError) as exc_info:
            await activate_unlimited_trial(db, user)

        # The original vendor error must still be reachable for diagnostics.
        assert isinstance(exc_info.value.__cause__, ArtemidaInsufficientBalance)


@pytest.mark.asyncio
async def test_activation_pings_admins(monkeypatch):
    """A successful premium-trial activation notifies admins (the same notification as
    a normal trial), so vendor trials show up in the admin chat — not only the
    low-balance alert."""
    from app.services import unlimited_trial_service as mod

    notify = AsyncMock()
    monkeypatch.setattr(mod, '_notify_admins_trial_activation', notify)
    _configure_artemida(monkeypatch)

    async with memory_session(monkeypatch, _TABLES) as db:
        await _make_trial_tariff(db)
        user = await _make_verified_user(db)

        subscription = await activate_unlimited_trial(db, user)

        notify.assert_awaited_once()
        args, _kwargs = notify.await_args
        assert args[2].id == subscription.id  # (db, user, subscription, bot=...)
