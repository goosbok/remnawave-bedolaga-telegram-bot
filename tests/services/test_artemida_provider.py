from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import SubscriptionStatus
from app.external.artemida_api import ArtemidaAPIError, ArtemidaInsufficientBalance
from app.services.providers import get_provider
from app.services.providers.artemida import ArtemidaProvider


def _tariff(provider='artemida', device_limit=3):
    return SimpleNamespace(id=7, provider=provider, device_limit=device_limit, traffic_limit_gb=0, provider_opts={})


def _subscription(device_limit=3, tariff=None, end_date=None, external_ref=None):
    return SimpleNamespace(
        id=42,
        user_id=1,
        tariff=tariff or _tariff(),
        external_ref=external_ref,
        external_provider=None,
        subscription_url=None,
        end_date=end_date if end_date is not None else datetime.now(UTC),
        device_limit=device_limit,
        status='pending',
    )


def _ctx(obj):
    class _C:
        async def __aenter__(self_):
            return obj

        async def __aexit__(self_, *a):
            return False

    return _C()


def test_get_provider_defaults_to_remnawave():
    assert get_provider(_tariff(provider='remnawave')).name == 'remnawave'


def test_get_provider_artemida():
    assert get_provider(_tariff(provider='artemida')).name == 'artemida'


@pytest.mark.asyncio
async def test_provision_calls_client_and_sets_fields(monkeypatch):
    # device_limit starts at 2 but the tariff says 5 — a dropped assignment would leave it at 2.
    sub = _subscription(device_limit=2, tariff=_tariff(device_limit=5))
    client = AsyncMock()
    client.create_key.return_value = SimpleNamespace(
        id='key_9', devices=5, subscription_url='https://vendor/x', expire_at=None
    )
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )

    await provider.provision(db=AsyncMock(), subscription=sub, days=30)

    client.create_key.assert_awaited_once()
    kwargs = client.create_key.await_args.kwargs
    assert kwargs['days'] == 30 and kwargs['devices'] == 5
    assert kwargs['customer_ref'] == '42'
    assert kwargs['idempotency_key'] == 'sub-42-provision'
    assert sub.external_ref == 'key_9'
    assert sub.external_provider == 'artemida'
    assert sub.subscription_url == 'https://sub.max/a/key_9'
    assert sub.device_limit == 5
    assert sub.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_provision_raises_when_rebrand_base_url_missing(monkeypatch):
    sub = _subscription()
    client = AsyncMock()
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))
    monkeypatch.setattr('app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', '', raising=False)

    with pytest.raises(ArtemidaAPIError):
        await provider.provision(db=AsyncMock(), subscription=sub, days=30)

    client.create_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_renew_uses_end_date_idempotency_key_and_sets_device_limit():
    end_date = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    sub = _subscription(end_date=end_date, external_ref='key_9')
    client = AsyncMock()
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))

    await provider.update(db=AsyncMock(), subscription=sub, days=30, devices=4)

    client.renew_key.assert_awaited_once()
    kwargs = client.renew_key.await_args.kwargs
    assert kwargs['idempotency_key'] == f'sub-42-renew-{int(end_date.timestamp())}'
    assert sub.device_limit == 4


@pytest.mark.asyncio
async def test_update_upgrade_only_uses_end_date_idempotency_key_and_sets_device_limit():
    end_date = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    sub = _subscription(end_date=end_date, external_ref='key_9')
    client = AsyncMock()
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))

    await provider.update(db=AsyncMock(), subscription=sub, devices=7)

    client.upgrade_key.assert_awaited_once()
    kwargs = client.upgrade_key.await_args.kwargs
    assert kwargs['idempotency_key'] == f'sub-42-upgrade-7-{int(end_date.timestamp())}'
    assert sub.device_limit == 7


@pytest.mark.asyncio
async def test_update_without_external_ref_does_not_call_client():
    sub = _subscription(external_ref=None)
    client = AsyncMock()
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))

    await provider.update(db=AsyncMock(), subscription=sub, days=30)

    client.renew_key.assert_not_awaited()
    client.upgrade_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_revoke_calls_client_with_key():
    sub = _subscription(external_ref='key_9')
    client = AsyncMock()
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))

    await provider.revoke(db=AsyncMock(), subscription=sub)

    client.revoke_key.assert_awaited_once()
    kwargs = client.revoke_key.await_args.kwargs
    assert kwargs['idempotency_key'] == 'sub-42-revoke'


@pytest.mark.asyncio
async def test_revoke_without_external_ref_does_not_call_client():
    sub = _subscription(external_ref=None)
    client = AsyncMock()
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))

    await provider.revoke(db=AsyncMock(), subscription=sub)

    client.revoke_key.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_usage_updates_device_limit_and_subscription_url(monkeypatch):
    sub = _subscription(external_ref='key_9', device_limit=3)
    client = AsyncMock()
    client.get_key.return_value = SimpleNamespace(id='key_9', devices=9, status='ACTIVE', subscription_url='https://x')
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )

    await provider.sync_usage(db=AsyncMock(), subscription=sub)

    assert sub.device_limit == 9
    assert sub.subscription_url == 'https://sub.max/a/key_9'


@pytest.mark.asyncio
async def test_provision_reraises_insufficient_balance_and_leaves_subscription_unchanged(monkeypatch):
    sub = _subscription()
    client = AsyncMock()
    client.create_key.side_effect = ArtemidaInsufficientBalance('no funds')
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )

    with pytest.raises(ArtemidaInsufficientBalance):
        await provider.provision(db=AsyncMock(), subscription=sub, days=30)

    assert sub.external_ref is None
    assert sub.external_provider is None
    assert sub.status == 'pending'
