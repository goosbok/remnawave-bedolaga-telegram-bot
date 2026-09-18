"""Dispatch seam: artemida-tariff provisioning routes to ArtemidaProvider.

Covers Task 5 of the Artemida vendor-integration plan — the four generic
"push desired state to the panel" entry points in SubscriptionService must
early-return to the provider for provider='artemida' tariffs, leaving the
remnawave body untouched for everything else.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import PromoGroup, Subscription, Tariff, tariff_promo_groups
from app.external.artemida_api import ArtemidaAPIError
from app.services.subscription_service import SubscriptionService
from tests.fixtures.sqlite_memory import memory_session


_TARIFF_TABLES = (Tariff.__table__, PromoGroup.__table__, tariff_promo_groups, Subscription.__table__)


@pytest.mark.asyncio
async def test_create_dispatches_to_artemida_provision(monkeypatch):
    provider = SimpleNamespace(name='artemida', provision=AsyncMock(), sync_usage=AsyncMock())
    service = SubscriptionService()
    monkeypatch.setattr(service, '_artemida_provider_or_none', AsyncMock(return_value=provider))
    sub = SimpleNamespace(
        id=42, user_id=1, end_date=None, subscription_url='https://sub.max/a/key_1', external_ref='key_1'
    )
    result = await service.create_remnawave_user(db=AsyncMock(), subscription=sub)
    provider.provision.assert_awaited_once()
    # create/update_remnawave_user return None only on FAILURE — callers do
    # `if not rem_user: raise`, so a successful artemida dispatch must be truthy.
    assert result
    assert bool(result) is True


@pytest.mark.asyncio
async def test_update_dispatches_to_sync_usage_not_renew(monkeypatch):
    provider = SimpleNamespace(name='artemida', provision=AsyncMock(), sync_usage=AsyncMock(), update=AsyncMock())
    service = SubscriptionService()
    monkeypatch.setattr(service, '_artemida_provider_or_none', AsyncMock(return_value=provider))
    sub = SimpleNamespace(
        id=42, user_id=1, end_date=None, subscription_url='https://sub.max/a/key_1', external_ref='key_1'
    )
    result = await service.update_remnawave_user(db=AsyncMock(), subscription=sub)
    provider.sync_usage.assert_awaited_once()
    provider.update.assert_not_awaited()  # generic update must NOT trigger a paid renew
    # `if result is None: requeue` in the retry queue — must not be None on success.
    assert result
    assert bool(result) is True


@pytest.mark.asyncio
async def test_revoke_is_noop_for_artemida(monkeypatch):
    """revoke_subscription's remnawave semantics are link/password ROTATION for an
    active customer (cooldown-protected "reissue link" buttons), not cancellation.
    Artemida has no rotation primitive — only create/revoke of the whole key — so
    calling provider.revoke() here would delete a paying customer's key. Must be
    a no-op that reports success without touching the vendor."""
    provider = SimpleNamespace(name='artemida', revoke=AsyncMock())
    service = SubscriptionService()
    monkeypatch.setattr(service, '_artemida_provider_or_none', AsyncMock(return_value=provider))
    sub = SimpleNamespace(id=42, user_id=1, subscription_url='https://sub.max/a/key_1')
    result = await service.revoke_subscription(db=AsyncMock(), subscription=sub)
    provider.revoke.assert_not_awaited()
    assert result == 'https://sub.max/a/key_1'


@pytest.mark.asyncio
async def test_sync_dispatches_to_artemida(monkeypatch):
    provider = SimpleNamespace(name='artemida', sync_usage=AsyncMock())
    service = SubscriptionService()
    monkeypatch.setattr(service, '_artemida_provider_or_none', AsyncMock(return_value=provider))
    sub = SimpleNamespace(id=42, user_id=1)
    result = await service.sync_subscription_usage(db=AsyncMock(), subscription=sub)
    provider.sync_usage.assert_awaited_once()
    assert result is True


@pytest.mark.asyncio
async def test_remnawave_tariff_not_dispatched(monkeypatch):
    # resolution returns None -> remnawave body runs; make it bail safely.
    service = SubscriptionService()
    monkeypatch.setattr(service, '_artemida_provider_or_none', AsyncMock(return_value=None))
    monkeypatch.setattr('app.services.subscription_service.get_user_by_id', AsyncMock(return_value=None))
    sub = SimpleNamespace(id=1, user_id=1)
    result = await service.create_remnawave_user(db=AsyncMock(), subscription=sub)
    assert result is None  # remnawave body returned None because user not found


@pytest.mark.asyncio
async def test_resolver_raises_when_artemida_tariff_but_disabled(monkeypatch):
    """Backstop: an artemida tariff resolved with ARTEMIDA_ENABLED=False must raise,
    not silently fall through to the remnawave body (which would try to provision
    the wrong vendor entirely)."""
    monkeypatch.setattr('app.services.subscription_service.settings.ARTEMIDA_ENABLED', False, raising=False)

    service = SubscriptionService()
    sub = SimpleNamespace(id=42, user_id=1, tariff_id=7, tariff=None)

    db = AsyncMock()
    db.scalar = AsyncMock(return_value='artemida')

    # subscription is a plain SimpleNamespace, not a mapped ORM instance, so
    # sa_inspect(subscription) blows up inside the resolver's try/except —
    # exercised here to confirm that path degrades to the light db.scalar() lookup.
    with pytest.raises(ArtemidaAPIError):
        await service._artemida_provider_or_none(db, sub)


# ---------------------------------------------------------------------------
# Real-DB resolver coverage — the tests above monkeypatch
# `_artemida_provider_or_none` itself, so its actual body (the
# unloaded-relationship / scalar-column / refresh logic) is barely exercised.
# These three run it against genuine mapped Subscription+Tariff rows.
# ---------------------------------------------------------------------------


async def _tariff_and_subscription(db, *, provider: str) -> Subscription:
    # No explicit refresh(): Tariff.allowed_promo_groups is lazy='selectin', and
    # refreshing a Tariff instance would eagerly join promo_groups/tariff_promo_groups
    # tables this fixture doesn't create. expire_on_commit=False (see memory_session)
    # means the autogenerated tariff.id is already populated right after commit.
    tariff = Tariff(name=f'{provider} plan', period_prices={'30': 49900}, provider=provider)
    db.add(tariff)
    await db.commit()

    subscription = Subscription(
        user_id=1,
        end_date=datetime.now(UTC) + timedelta(days=30),
        tariff_id=tariff.id,
    )
    db.add(subscription)
    await db.commit()
    return subscription


@pytest.mark.asyncio
async def test_resolver_real_db_remnawave_tariff_returns_none(monkeypatch):
    async with memory_session(monkeypatch, _TARIFF_TABLES) as db:
        subscription = await _tariff_and_subscription(db, provider='remnawave')

        service = SubscriptionService()
        result = await service._artemida_provider_or_none(db, subscription)
        assert result is None


@pytest.mark.asyncio
async def test_resolver_real_db_artemida_enabled_returns_provider(monkeypatch):
    monkeypatch.setattr('app.services.subscription_service.settings.ARTEMIDA_ENABLED', True, raising=False)

    async with memory_session(monkeypatch, _TARIFF_TABLES) as db:
        subscription = await _tariff_and_subscription(db, provider='artemida')

        service = SubscriptionService()
        result = await service._artemida_provider_or_none(db, subscription)
        assert result is not None
        assert result.name == 'artemida'


@pytest.mark.asyncio
async def test_resolver_real_db_artemida_disabled_raises(monkeypatch):
    monkeypatch.setattr('app.services.subscription_service.settings.ARTEMIDA_ENABLED', False, raising=False)

    async with memory_session(monkeypatch, _TARIFF_TABLES) as db:
        subscription = await _tariff_and_subscription(db, provider='artemida')

        service = SubscriptionService()
        with pytest.raises(ArtemidaAPIError):
            await service._artemida_provider_or_none(db, subscription)


# ---------------------------------------------------------------------------
# sync_remnawave_user provider-aware routing (cross-cutting fix): artemida has
# no remnawave_id, so the create-vs-update decision must route by external_ref
# instead — otherwise an already-provisioned artemida sub would re-provision
# (create_remnawave_user -> provider.provision -> client.create_key with the
# frozen sub-{id}-provision idempotency key) on every renewal/referral-bonus
# sync instead of a safe, free sync_usage refresh.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_remnawave_user_existing_artemida_routes_to_update(monkeypatch):
    provider = SimpleNamespace(name='artemida')
    service = SubscriptionService()
    monkeypatch.setattr(service, '_artemida_provider_or_none', AsyncMock(return_value=provider))
    create = AsyncMock()
    update = AsyncMock()
    monkeypatch.setattr(service, 'create_remnawave_user', create)
    monkeypatch.setattr(service, 'update_remnawave_user', update)

    sub = SimpleNamespace(id=42, user_id=1, remnawave_id=None, external_ref='key_1')
    await service.sync_remnawave_user(db=AsyncMock(), subscription=sub)

    update.assert_awaited_once()
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_remnawave_user_new_artemida_routes_to_create(monkeypatch):
    provider = SimpleNamespace(name='artemida')
    service = SubscriptionService()
    monkeypatch.setattr(service, '_artemida_provider_or_none', AsyncMock(return_value=provider))
    create = AsyncMock()
    update = AsyncMock()
    monkeypatch.setattr(service, 'create_remnawave_user', create)
    monkeypatch.setattr(service, 'update_remnawave_user', update)

    sub = SimpleNamespace(id=42, user_id=1, remnawave_id=None, external_ref=None)
    await service.sync_remnawave_user(db=AsyncMock(), subscription=sub)

    create.assert_awaited_once()
    update.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_remnawave_user_remnawave_unaffected(monkeypatch):
    """resolution returns None for a remnawave tariff -> the pre-existing
    remnawave_id-based create-vs-update routing must stay exactly as before
    this fix. Multi-tariff is enabled here so panel_id comes straight off the
    subscription (no get_user_by_id/DB round trip needed for this check)."""
    service = SubscriptionService()
    monkeypatch.setattr(service, '_artemida_provider_or_none', AsyncMock(return_value=None))
    monkeypatch.setattr('app.services.subscription_service.settings.MULTI_TARIFF_ENABLED', True, raising=False)
    monkeypatch.setattr('app.services.subscription_service.settings.SALES_MODE', 'tariffs', raising=False)

    create = AsyncMock()
    update = AsyncMock()
    monkeypatch.setattr(service, 'create_remnawave_user', create)
    monkeypatch.setattr(service, 'update_remnawave_user', update)

    no_panel_id_sub = SimpleNamespace(id=1, user_id=1, remnawave_id=None)
    await service.sync_remnawave_user(db=AsyncMock(), subscription=no_panel_id_sub)
    create.assert_awaited_once()
    update.assert_not_awaited()

    create.reset_mock()
    update.reset_mock()

    known_panel_id_sub = SimpleNamespace(id=2, user_id=1, remnawave_id=123)
    await service.sync_remnawave_user(db=AsyncMock(), subscription=known_panel_id_sub)
    update.assert_awaited_once()
    create.assert_not_awaited()
