"""Manual vendor-to-vendor subscription swap, for the REMAINING term, same client link.

Mirrors the setup of ``tests/integration/test_artemida_provision_e2e.py``: a real
``Tariff``/``Subscription`` persisted in an in-memory SQLite session, with the vendor
HTTP call faked at the ``ArtemidaClient`` boundary (no network). A second, fake
provider (``vendor2``) is registered through the real ``register_provider`` hook to
stand in for whatever vendor a subscription is being moved TO.
"""

from datetime import UTC, datetime, timedelta

import pytest

from app.database.models import PromoGroup, Subscription, SubscriptionStatus, Tariff, tariff_promo_groups
from app.services.provider_swap_service import ProviderSwapError, move_subscription_to_provider
from app.services.providers import _PROVIDERS, register_provider
from tests.fixtures.sqlite_memory import memory_session


# Mirrors _TARIFF_TABLES in tests/services/test_subscription_provider_dispatch.py:
# Tariff.allowed_promo_groups is lazy='selectin', so persisting/refreshing a Tariff
# implicitly touches PromoGroup + the tariff_promo_groups association table too.
_TABLES = (Tariff.__table__, PromoGroup.__table__, tariff_promo_groups, Subscription.__table__)


class _FakeArtemidaClient:
    """Stand-in for ArtemidaClient: no network, no live vendor key.

    Only ``revoke_key`` is exercised here (the OLD-vendor path); ``create_key`` is not
    expected to be called since the subscription starts already provisioned on artemida.
    """

    def __init__(self):
        self.revoke_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def revoke_key(self, key_id, *, idempotency_key):
        self.revoke_calls.append({'key_id': key_id, 'idempotency_key': idempotency_key})
        return {'status': 'REVOKED'}


class FakeVendor2Provider:
    """Fake second vendor a subscription can be swapped ONTO."""

    name = 'vendor2'

    def __init__(self, *, fail_provision: bool = False):
        self.fail_provision = fail_provision
        self.provision_calls: list[dict] = []
        self.revoke_calls: list[str | None] = []

    async def provision(self, *, db, subscription, days):
        self.provision_calls.append({'subscription_id': subscription.id, 'days': days})
        if self.fail_provision:
            raise RuntimeError('vendor2 provision failed')
        subscription.external_provider = 'vendor2'
        subscription.external_ref = 'key_v2'
        subscription.device_limit = getattr(subscription.tariff, 'device_limit', 2)
        # Rebuilt from the UNCHANGED public_token — this is what keeps the client link stable.
        subscription.subscription_url = f'https://sub.max/a/{subscription.public_token}'

    async def revoke(self, *, db, subscription):
        self.revoke_calls.append(subscription.external_ref)


@pytest.fixture
def restore_providers():
    """register_provider mutates the module-level _PROVIDERS registry — restore it after."""
    original = dict(_PROVIDERS)
    yield
    _PROVIDERS.clear()
    _PROVIDERS.update(original)


async def _make_artemida_subscription(db, *, device_limit: int = 5, end_in_days: int = 30) -> Subscription:
    tariff = Tariff(
        name='Максимум', provider='artemida', device_limit=device_limit, traffic_limit_gb=0,
        period_prices={'30': 49900},
    )
    db.add(tariff)
    await db.flush()

    now = datetime.now(UTC)
    subscription = Subscription(
        user_id=1,
        tariff_id=tariff.id,
        start_date=now,
        # A generous margin over end_in_days so the "remaining days" computation
        # (now()-based, per the manual-op contract) doesn't flake on slow test runs.
        end_date=now + timedelta(days=end_in_days, hours=1),
        status=SubscriptionStatus.ACTIVE.value,
        external_provider='artemida',
        external_ref='key_old',
        public_token='tok_1',
        subscription_url='https://sub.max/a/tok_1',
        device_limit=device_limit,
    )
    db.add(subscription)
    await db.commit()
    await db.refresh(subscription)
    return subscription


@pytest.mark.asyncio
async def test_swap_repoints_and_keeps_link(monkeypatch, restore_providers):
    fake_client = _FakeArtemidaClient()
    monkeypatch.setattr('app.services.providers.artemida.ArtemidaClient', lambda *a, **k: fake_client)

    fake_vendor2 = FakeVendor2Provider()
    register_provider('vendor2', lambda: fake_vendor2)

    async with memory_session(monkeypatch, _TABLES) as db:
        subscription = await _make_artemida_subscription(db)
        sub_id = subscription.id

        await move_subscription_to_provider(db, subscription, 'vendor2')

        assert subscription.external_provider == 'vendor2'
        assert subscription.external_ref == 'key_v2'
        assert subscription.public_token == 'tok_1'  # UNCHANGED
        assert subscription.subscription_url == 'https://sub.max/a/tok_1'  # UNCHANGED

        # The new vendor was provisioned for (approximately) the remaining term.
        assert fake_vendor2.provision_calls
        assert fake_vendor2.provision_calls[0]['subscription_id'] == sub_id
        assert 29 <= fake_vendor2.provision_calls[0]['days'] <= 30

        # The OLD (artemida) key was revoked, keyed by the OLD ref, not the new one.
        assert fake_client.revoke_calls == [
            {'key_id': 'key_old', 'idempotency_key': f'sub-{sub_id}-revoke'}
        ]

        # Persisted, not just in-memory.
        await db.refresh(subscription)
        assert subscription.external_provider == 'vendor2'
        assert subscription.external_ref == 'key_v2'


@pytest.mark.asyncio
async def test_swap_provision_failure_leaves_old_vendor(monkeypatch, restore_providers):
    fake_client = _FakeArtemidaClient()
    monkeypatch.setattr('app.services.providers.artemida.ArtemidaClient', lambda *a, **k: fake_client)

    fake_vendor2 = FakeVendor2Provider(fail_provision=True)
    register_provider('vendor2', lambda: fake_vendor2)

    async with memory_session(monkeypatch, _TABLES) as db:
        subscription = await _make_artemida_subscription(db)

        with pytest.raises(RuntimeError, match='vendor2 provision failed'):
            await move_subscription_to_provider(db, subscription, 'vendor2')

        assert subscription.external_provider == 'artemida'
        assert subscription.external_ref == 'key_old'
        assert subscription.subscription_url == 'https://sub.max/a/tok_1'
        assert subscription.public_token == 'tok_1'

        # The old vendor must NOT have been touched: provision never succeeded.
        assert fake_client.revoke_calls == []


@pytest.mark.asyncio
async def test_swap_to_same_provider_is_noop(monkeypatch, restore_providers):
    fake_client = _FakeArtemidaClient()
    monkeypatch.setattr('app.services.providers.artemida.ArtemidaClient', lambda *a, **k: fake_client)

    async with memory_session(monkeypatch, _TABLES) as db:
        subscription = await _make_artemida_subscription(db)

        await move_subscription_to_provider(db, subscription, 'artemida')

        assert subscription.external_provider == 'artemida'
        assert subscription.external_ref == 'key_old'
        assert subscription.subscription_url == 'https://sub.max/a/tok_1'
        # No-op: the old (== new) vendor was never called at all.
        assert fake_client.revoke_calls == []


@pytest.mark.asyncio
async def test_swap_to_unknown_provider_raises(monkeypatch, restore_providers):
    async with memory_session(monkeypatch, _TABLES) as db:
        subscription = await _make_artemida_subscription(db)

        with pytest.raises(ProviderSwapError):
            await move_subscription_to_provider(db, subscription, 'no_such_vendor')

        with pytest.raises(ProviderSwapError):
            await move_subscription_to_provider(db, subscription, 'remnawave')

        # Unchanged after either rejected attempt.
        assert subscription.external_provider == 'artemida'
        assert subscription.external_ref == 'key_old'
