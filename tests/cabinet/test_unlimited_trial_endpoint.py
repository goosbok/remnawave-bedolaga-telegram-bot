"""Cabinet endpoint for the unlimited (Artemida) trial — Task 6 of the two-trials feature.

Mirrors the limited trial's cabinet endpoints in
``app/cabinet/routes/subscription_modules/purchase.py``:

- ``POST /subscription/trial/unlimited`` activates the unlimited (Artemida) trial,
  gated by ``unlimited_trial_service.unlimited_trial_available(user)``.
- ``GET /subscription/trial`` (trial-availability info) now also reports an
  ``unlimited`` eligibility flag, alongside the existing (limited-trial) fields.

Uses the same real-DB approach as
``tests/integration/test_unlimited_trial_activation.py`` (in-memory SQLite,
vendor HTTP faked at the ``ArtemidaClient`` boundary) and calls the cabinet
route handlers directly with explicit ``user=``/``db=`` kwargs, the way
``tests/cabinet/test_free_tariff_purchase.py`` already does for this same
``purchase.py`` module.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
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
from tests.fixtures.sqlite_memory import memory_session


# Mirrors _TABLES in tests/integration/test_unlimited_trial_activation.py: the
# rollback path's db.delete(subscription) loads DiscountOffer/PlategaSubscription/
# LavaSubscription/SubscriptionEvent during flush (backrefs without
# passive_deletes=True), so all of them must exist even though this test never
# triggers rollback directly.
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


def _configure_artemida(
    monkeypatch,
    *,
    enabled: bool = True,
    trial_enabled: bool = True,
    fail_with: Exception | None = None,
) -> _FakeArtemidaClient:
    monkeypatch.setattr('app.config.settings.ARTEMIDA_ENABLED', enabled, raising=False)
    monkeypatch.setattr('app.config.settings.ARTEMIDA_TRIAL_ENABLED', trial_enabled, raising=False)
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )
    fake_client = _FakeArtemidaClient(fail_with=fail_with)
    monkeypatch.setattr('app.services.providers.artemida.ArtemidaClient', lambda *a, **k: fake_client)
    return fake_client


async def _make_user(db, **overrides) -> User:
    defaults: dict = dict(email='trial-user@example.com', email_verified=True, auth_type='email')
    defaults.update(overrides)
    user = User(**defaults)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    # has_used_trial()/unlimited_trial_available() read user.subscriptions
    # synchronously — must be eagerly loaded before any gate check (same
    # precondition documented on User.is_trial_already_used).
    await db.refresh(user, ['subscriptions'])
    return user


async def _make_trial_tariff(db, **overrides) -> Tariff:
    defaults: dict = dict(
        name='Безлимит-триал',
        provider='artemida',
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


def _old_used_subscription(user_id: int, tariff_id: int) -> Subscription:
    """An already-used (expired, non-pending) unlimited trial for this user."""
    now = datetime.now(UTC)
    return Subscription(
        user_id=user_id,
        tariff_id=tariff_id,
        status='expired',
        is_trial=True,
        external_provider='artemida',
        external_ref='key_old',
        start_date=now - timedelta(days=2),
        end_date=now - timedelta(days=1),
        traffic_limit_gb=0,
        device_limit=2,
        remnawave_short_id='old1',
    )


# ── POST /subscription/trial/unlimited ──────────────────────────────────────


@pytest.mark.asyncio
async def test_eligible_user_activates_unlimited_trial(monkeypatch):
    from app.cabinet.routes.subscription_modules.purchase import activate_unlimited_trial

    fake_client = _configure_artemida(monkeypatch)

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff = await _make_trial_tariff(db)
        user = await _make_user(db)

        response = await activate_unlimited_trial(user=user, db=db)

        assert response.is_trial is True

        rows = (await db.execute(select(Subscription).where(Subscription.user_id == user.id))).scalars().all()
        assert len(rows) == 1
        subscription = rows[0]
        assert subscription.external_provider == 'artemida'
        assert subscription.external_ref == 'key_trial_1'
        assert subscription.tariff_id == tariff.id
        assert fake_client.kwargs is not None
        assert fake_client.kwargs['devices'] == tariff.device_limit

        await db.refresh(user, ['subscriptions'])
        assert user.has_used_trial('unlimited') is True


@pytest.mark.asyncio
async def test_flag_off_rejects_without_activating(monkeypatch):
    from app.cabinet.routes.subscription_modules.purchase import activate_unlimited_trial

    fake_client = _configure_artemida(monkeypatch, trial_enabled=False)

    async with memory_session(monkeypatch, _TABLES) as db:
        await _make_trial_tariff(db)
        user = await _make_user(db)

        with pytest.raises(HTTPException) as exc_info:
            await activate_unlimited_trial(user=user, db=db)

        assert 400 <= exc_info.value.status_code < 500

        assert fake_client.kwargs is None
        remaining = (await db.execute(select(Subscription))).scalars().all()
        assert remaining == []


@pytest.mark.asyncio
async def test_unverified_user_rejects_without_activating(monkeypatch):
    from app.cabinet.routes.subscription_modules.purchase import activate_unlimited_trial

    fake_client = _configure_artemida(monkeypatch)

    async with memory_session(monkeypatch, _TABLES) as db:
        await _make_trial_tariff(db)
        user = await _make_user(db, email=None, email_verified=False, auth_type='telegram', telegram_id=None)

        with pytest.raises(HTTPException) as exc_info:
            await activate_unlimited_trial(user=user, db=db)

        assert 400 <= exc_info.value.status_code < 500

        assert fake_client.kwargs is None
        remaining = (await db.execute(select(Subscription))).scalars().all()
        assert remaining == []


@pytest.mark.asyncio
async def test_already_used_unlimited_trial_rejects_without_activating(monkeypatch):
    from app.cabinet.routes.subscription_modules.purchase import activate_unlimited_trial

    fake_client = _configure_artemida(monkeypatch)

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff = await _make_trial_tariff(db)
        user = await _make_user(db)

        db.add(_old_used_subscription(user.id, tariff.id))
        await db.commit()
        await db.refresh(user, ['subscriptions'])

        with pytest.raises(HTTPException) as exc_info:
            await activate_unlimited_trial(user=user, db=db)

        assert 400 <= exc_info.value.status_code < 500

        assert fake_client.kwargs is None
        rows = (await db.execute(select(Subscription).where(Subscription.user_id == user.id))).scalars().all()
        assert len(rows) == 1  # still just the old, already-used one — no new row


# ── GET /subscription/trial — unlimited eligibility flag ───────────────────


@pytest.mark.asyncio
async def test_trial_info_reports_unlimited_available(monkeypatch):
    from app.cabinet.routes.subscription_modules.purchase import get_trial_info

    _configure_artemida(monkeypatch)

    async with memory_session(monkeypatch, _TABLES) as db:
        await _make_trial_tariff(db)
        user = await _make_user(db)

        response = await get_trial_info(user=user, db=db)

    assert response.unlimited is True


@pytest.mark.asyncio
async def test_trial_info_reports_unlimited_unavailable_when_flag_off(monkeypatch):
    from app.cabinet.routes.subscription_modules.purchase import get_trial_info

    _configure_artemida(monkeypatch, trial_enabled=False)

    async with memory_session(monkeypatch, _TABLES) as db:
        await _make_trial_tariff(db)
        user = await _make_user(db)

        response = await get_trial_info(user=user, db=db)

    assert response.unlimited is False


@pytest.mark.asyncio
async def test_trial_info_reports_unlimited_unavailable_when_already_used(monkeypatch):
    from app.cabinet.routes.subscription_modules.purchase import get_trial_info

    _configure_artemida(monkeypatch)

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff = await _make_trial_tariff(db)
        user = await _make_user(db)

        db.add(_old_used_subscription(user.id, tariff.id))
        await db.commit()
        await db.refresh(user, ['subscriptions'])

        response = await get_trial_info(user=user, db=db)

    assert response.unlimited is False
