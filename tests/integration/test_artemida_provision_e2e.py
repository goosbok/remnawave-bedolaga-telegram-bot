"""Real-DB, in-process end-to-end test for Artemida provisioning.

The unit-level dispatch tests in ``tests/services/test_subscription_provider_dispatch.py``
monkeypatch ``_external_provider_or_none`` itself (or drive the resolver against a
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

    def __init__(self):
        self.create_key_kwargs = None
        self.create_trial_called = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def create_key(self, **kwargs):
        self.create_key_kwargs = kwargs
        self.kwargs = kwargs  # back-compat alias for the paid e2e assertions
        return SimpleNamespace(
            id='key_live_1',
            devices=kwargs['devices'],
            subscription_url='https://vendor/x',
            expire_at=None,
        )

    async def create_trial(self, **kwargs):
        self.create_trial_called = True
        self.trial_kwargs = kwargs
        return SimpleNamespace(
            id='trial_key_1',
            devices=2,
            subscription_url='https://vendor/trial',
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
            is_trial=False,
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


@pytest.mark.asyncio
async def test_artemida_provision_rounds_subsecond_drift(monkeypatch):
    """Regression for the prod incident: start_date/end_date are captured by
    separate now() calls at purchase, so a nominal 30-day term lands a few ms
    short (29d 23:59:59.995). ``(end-start).days`` floored that to 29 — a period
    the vendor rejects ("параметры покупки вне допустимого диапазона", since it
    only accepts {7,30,60,90}). The rounded value must be sent as 30."""
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

        # Exact timestamps observed on the broken prod subscription 216.
        start = datetime(2026, 9, 21, 14, 10, 32, 856013, tzinfo=UTC)
        end = datetime(2026, 10, 21, 14, 10, 32, 851291, tzinfo=UTC)  # 29d 23:59:59.995278
        subscription = Subscription(
            user_id=1,
            tariff_id=tariff.id,
            start_date=start,
            end_date=end,
            is_trial=False,
            status=SubscriptionStatus.PENDING.value,
        )
        db.add(subscription)
        await db.commit()
        await db.refresh(subscription)

        await SubscriptionService().create_remnawave_user(db, subscription)

        assert fake_client.create_key_kwargs is not None
        assert fake_client.create_key_kwargs['days'] == 30  # rounded, NOT floored to 29


@pytest.mark.asyncio
async def test_artemida_trial_uses_create_trial(monkeypatch):
    """A trial must hit POST /trial (create_trial), NOT create_key: the vendor
    only accepts the discrete paid periods {7,30,60,90}, so a 1-day trial routed
    through create_key is rejected outright."""
    monkeypatch.setattr('app.config.settings.ARTEMIDA_ENABLED', True, raising=False)
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )
    fake_client = _FakeArtemidaClient()
    monkeypatch.setattr('app.services.providers.artemida.ArtemidaClient', lambda *a, **k: fake_client)

    async with memory_session(monkeypatch, _TABLES) as db:
        tariff = Tariff(
            name='Премиум подписка на 1 день',
            provider='artemida',
            device_limit=2,
            traffic_limit_gb=0,
            period_prices={'30': 0},
            is_trial_available=True,
        )
        db.add(tariff)
        await db.flush()

        now = datetime.now(UTC)
        subscription = Subscription(
            user_id=1,
            tariff_id=tariff.id,
            start_date=now,
            end_date=now + timedelta(days=1),
            is_trial=True,
            status=SubscriptionStatus.PENDING.value,
        )
        db.add(subscription)
        await db.commit()
        await db.refresh(subscription)

        result = await SubscriptionService().create_remnawave_user(db, subscription)
        assert result

        assert fake_client.create_trial_called is True
        assert fake_client.create_key_kwargs is None  # create_key must NOT run for a trial

        await db.refresh(subscription)
        assert subscription.external_provider == 'artemida'
        assert subscription.external_ref == 'trial_key_1'
        assert subscription.public_token
        assert subscription.subscription_url == f'https://sub.max/a/{subscription.public_token}'
