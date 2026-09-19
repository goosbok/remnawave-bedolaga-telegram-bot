from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.database.models import SubscriptionStatus
from app.external.artemida_api import ArtemidaAPIError, ArtemidaInsufficientBalance
from app.services.providers import get_provider
from app.services.providers.artemida import ArtemidaProvider, _chunk_days


def _tariff(provider='artemida', device_limit=3):
    return SimpleNamespace(id=7, provider=provider, device_limit=device_limit, traffic_limit_gb=0, provider_opts={})


def _subscription(device_limit=3, tariff=None, end_date=None, external_ref=None, public_token=None):
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
        public_token=public_token,
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


def test_chunk_days_at_or_under_max_is_a_single_chunk():
    assert _chunk_days(30) == [30]
    assert _chunk_days(90) == [90]


def test_chunk_days_splits_into_at_most_90_day_pieces():
    assert _chunk_days(180) == [90, 90]
    assert _chunk_days(360) == [90, 90, 90, 90]
    assert _chunk_days(100) == [90, 10]
    assert _chunk_days(91) == [90, 1]
    assert _chunk_days(1) == [1]


def test_chunk_days_rejects_non_positive_days():
    with pytest.raises(ValueError, match='days'):
        _chunk_days(0)
    with pytest.raises(ValueError, match='days'):
        _chunk_days(-1)


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
    assert kwargs['idempotency_key'] == 'sub-42-provision-0'
    client.renew_key.assert_not_awaited()
    assert sub.external_ref == 'key_9'
    assert sub.external_provider == 'artemida'
    assert sub.public_token
    assert sub.subscription_url == f'https://sub.max/a/{sub.public_token}'
    assert sub.device_limit == 5
    assert sub.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_provision_sets_public_token_and_builds_url_from_it(monkeypatch):
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )
    sub = _subscription()
    sub.public_token = None
    client = AsyncMock()
    client.create_key.return_value = SimpleNamespace(
        id='key_9', devices=3, subscription_url='https://vendor/x', expire_at=None
    )
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))

    await provider.provision(db=AsyncMock(), subscription=sub, days=30)

    assert sub.public_token  # generated, non-empty
    assert sub.subscription_url == f'https://sub.max/a/{sub.public_token}'
    assert '/key_9' not in sub.subscription_url  # NOT the vendor key id

    # re-provision keeps the same token (set once)
    token1 = sub.public_token
    await provider.provision(db=AsyncMock(), subscription=sub, days=30)
    assert sub.public_token == token1


@pytest.mark.asyncio
async def test_provision_days_180_creates_one_90_day_key_and_renews_one_90_day_chunk(monkeypatch):
    sub = _subscription(tariff=_tariff(device_limit=5))
    client = AsyncMock()
    client.create_key.return_value = SimpleNamespace(
        id='key_9', devices=5, subscription_url='https://vendor/x', expire_at=None
    )
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )

    await provider.provision(db=AsyncMock(), subscription=sub, days=180)

    client.create_key.assert_awaited_once()
    create_kwargs = client.create_key.await_args.kwargs
    assert create_kwargs['days'] == 90
    assert create_kwargs['idempotency_key'] == 'sub-42-provision-0'

    client.renew_key.assert_awaited_once()
    renew_call = client.renew_key.await_args
    assert renew_call.args == ('key_9',)
    assert renew_call.kwargs['days'] == 90
    assert renew_call.kwargs['idempotency_key'] == 'sub-42-provision-1'

    assert sub.external_ref == 'key_9'
    assert sub.device_limit == 5
    assert sub.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_provision_days_360_creates_one_key_and_renews_three_90_day_chunks(monkeypatch):
    sub = _subscription(tariff=_tariff(device_limit=5))
    client = AsyncMock()
    client.create_key.return_value = SimpleNamespace(
        id='key_9', devices=5, subscription_url='https://vendor/x', expire_at=None
    )
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )

    await provider.provision(db=AsyncMock(), subscription=sub, days=360)

    create_kwargs = client.create_key.await_args.kwargs
    assert create_kwargs['days'] == 90
    assert create_kwargs['idempotency_key'] == 'sub-42-provision-0'

    assert client.renew_key.await_count == 3
    for i, call in enumerate(client.renew_key.await_args_list, start=1):
        assert call.args == ('key_9',)
        assert call.kwargs['days'] == 90
        assert call.kwargs['idempotency_key'] == f'sub-42-provision-{i}'

    assert sub.external_ref == 'key_9'
    assert sub.status == SubscriptionStatus.ACTIVE.value


@pytest.mark.asyncio
async def test_provision_days_100_creates_90_day_key_and_renews_10_day_remainder(monkeypatch):
    sub = _subscription(tariff=_tariff(device_limit=5))
    client = AsyncMock()
    client.create_key.return_value = SimpleNamespace(
        id='key_9', devices=5, subscription_url='https://vendor/x', expire_at=None
    )
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )

    await provider.provision(db=AsyncMock(), subscription=sub, days=100)

    assert client.create_key.await_args.kwargs['days'] == 90
    client.renew_key.assert_awaited_once()
    assert client.renew_key.await_args.kwargs['days'] == 10
    assert client.renew_key.await_args.kwargs['idempotency_key'] == 'sub-42-provision-1'


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
    assert kwargs['idempotency_key'] == f'sub-42-renew-{int(end_date.timestamp())}-0'
    assert kwargs['devices'] == 4
    assert sub.device_limit == 4


@pytest.mark.asyncio
async def test_update_renew_days_180_chunks_into_two_90_day_renew_calls():
    end_date = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    ts = int(end_date.timestamp())
    sub = _subscription(end_date=end_date, external_ref='key_9')
    client = AsyncMock()
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))

    await provider.update(db=AsyncMock(), subscription=sub, days=180, devices=4)

    assert client.renew_key.await_count == 2
    calls = client.renew_key.await_args_list

    assert calls[0].args == ('key_9',)
    assert calls[0].kwargs['days'] == 90
    assert calls[0].kwargs['devices'] == 4
    assert calls[0].kwargs['idempotency_key'] == f'sub-42-renew-{ts}-0'

    # devices is sent on EVERY chunk (not just the first): whether the vendor keeps
    # the device count unchanged when the field is omitted from a renew call is
    # unverified, so each chunk explicitly asserts the intended device count.
    assert calls[1].args == ('key_9',)
    assert calls[1].kwargs['days'] == 90
    assert calls[1].kwargs['devices'] == 4
    assert calls[1].kwargs['idempotency_key'] == f'sub-42-renew-{ts}-1'

    assert sub.device_limit == 4


@pytest.mark.asyncio
async def test_update_renew_reraises_when_second_chunk_fails_and_leaves_device_limit_unchanged():
    # 180 days = two 90-day renew chunks. Failing the 2nd must propagate and must NOT
    # apply subscription.device_limit — that assignment only happens after the loop,
    # so a mid-chunk failure must leave the subscription's own device_limit untouched
    # (mirrors test_provision_reraises_when_second_chunk_renew_fails_and_leaves_subscription_unchanged).
    end_date = datetime(2026, 10, 1, 12, 0, 0, tzinfo=UTC)
    sub = _subscription(end_date=end_date, external_ref='key_9', device_limit=3)
    client = AsyncMock()
    client.renew_key.side_effect = [None, ArtemidaInsufficientBalance('no funds')]
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))

    with pytest.raises(ArtemidaInsufficientBalance):
        await provider.update(db=AsyncMock(), subscription=sub, days=180, devices=4)

    assert client.renew_key.await_count == 2
    assert sub.device_limit == 3


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
    sub = _subscription(external_ref='key_9', device_limit=3, public_token='tok_abc')
    client = AsyncMock()
    client.get_key.return_value = SimpleNamespace(id='key_9', devices=9, status='ACTIVE', subscription_url='https://x')
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )

    await provider.sync_usage(db=AsyncMock(), subscription=sub)

    assert sub.device_limit == 9
    assert sub.subscription_url == 'https://sub.max/a/tok_abc'


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


@pytest.mark.asyncio
async def test_provision_reraises_when_second_chunk_renew_fails_and_leaves_subscription_unchanged(monkeypatch):
    # 180 days = create_key (chunk 0) + one renew_key (chunk 1). Failing that renew_key
    # call is failing the 2nd vendor call overall; it must propagate and the subscription
    # must be left exactly as it was before provisioning (the created key becomes an
    # orphaned, auto-expiring vendor key — acceptable).
    sub = _subscription(tariff=_tariff(device_limit=5))
    client = AsyncMock()
    client.create_key.return_value = SimpleNamespace(
        id='key_9', devices=5, subscription_url='https://vendor/x', expire_at=None
    )
    client.renew_key.side_effect = ArtemidaInsufficientBalance('no funds')
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )

    with pytest.raises(ArtemidaInsufficientBalance):
        await provider.provision(db=AsyncMock(), subscription=sub, days=180)

    client.create_key.assert_awaited_once()
    client.renew_key.assert_awaited_once()
    assert sub.external_ref is None
    assert sub.external_provider is None
    assert sub.status == 'pending'
