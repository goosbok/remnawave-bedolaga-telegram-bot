"""Real-DB, in-process end-to-end test for Artemida provisioning.

The unit-level dispatch tests in ``tests/services/test_subscription_provider_dispatch.py``
monkeypatch ``_artemida_provider_or_none`` itself (or drive the resolver against a
``SimpleNamespace`` subscription), so ``ArtemidaProvider.provision`` and
``SubscriptionService.create_remnawave_user``'s artemida branch are never exercised
together against a genuine mapped ``Tariff``/``Subscription`` through a real session.

This test closes that gap: a real ``Tariff(provider='artemida')`` + ``Subscription``
are persisted in an in-memory SQLite session, the vendor HTTP call is faked at the
``ArtemidaClient`` boundary (no network, no live key), and the real
``SubscriptionService().create_remnawave_user`` is called end-to-end.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.database.models import PromoGroup, Subscription, SubscriptionStatus, Tariff, tariff_promo_groups
from app.services.subscription_service import SubscriptionService
from tests.fixtures.sqlite_memory import memory_session


# Mirrors _TARIFF_TABLES in tests/services/test_subscription_provider_dispatch.py:
# Tariff.allowed_promo_groups is lazy='selectin', so persisting/refreshing a Tariff
# implicitly touches PromoGroup + the tariff_promo_groups association table too.
_TABLES = (Tariff.__table__, PromoGroup.__table__, tariff_promo_groups, Subscription.__table__)


class _FakeArtemidaClient:
    """Stand-in for ArtemidaClient: no network, no live vendor key."""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def create_key(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            id='key_live_1',
            devices=kwargs['devices'],
            subscription_url='https://vendor/x',
            expire_at=None,
        )


@pytest.mark.asyncio
async def test_artemida_provision_e2e(monkeypatch):
    monkeypatch.setattr('app.config.settings.ARTEMIDA_ENABLED', True, raising=False)
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )

    fake_client = _FakeArtemidaClient()
    monkeypatch.setattr('app.services.providers.artemida.ArtemidaClient', lambda *a, **k: fake_client)

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff = Tariff(
            name='Максимум', provider='artemida', device_limit=3, traffic_limit_gb=0, period_prices={'30': 49900}
        )
        db.add(tariff)
        await db.flush()

        now = datetime.now(UTC)
        subscription = Subscription(
            user_id=1,
            tariff_id=tariff.id,
            start_date=now,
            end_date=now + timedelta(days=30),
            status=SubscriptionStatus.PENDING.value,
        )
        db.add(subscription)
        await db.commit()
        await db.refresh(subscription)

        result = await SubscriptionService().create_remnawave_user(db, subscription)

        # create/update_remnawave_user return None only on FAILURE; the artemida
        # branch must return a truthy stand-in on success (see _ArtemidaProvisionResult).
        assert result

        await db.refresh(subscription)
        assert subscription.external_provider == 'artemida'
        assert subscription.external_ref == 'key_live_1'
        assert subscription.public_token
        assert subscription.subscription_url == f'https://sub.max/a/{subscription.public_token}'
        assert 'key_live_1' not in subscription.subscription_url  # NOT the vendor key id
        assert subscription.device_limit == 3
        assert subscription.status == SubscriptionStatus.ACTIVE.value

        # The vendor received the right call.
        assert fake_client.kwargs['devices'] == 3
        assert fake_client.kwargs['customer_ref'] == str(subscription.id)
        assert fake_client.kwargs['idempotency_key'] == f'sub-{subscription.id}-provision-0'
        # _provision_days() now derives from the fixed start_date/end_date pair (not
        # from "now"), so with start_date and end_date exactly 30 days apart this is
        # exact rather than approximate — no elapsed-time slack needed.
        assert fake_client.kwargs['days'] == 30
