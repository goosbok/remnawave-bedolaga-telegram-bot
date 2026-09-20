"""Autopay seam (MonitoringService._process_autopayments): an external-vendor autopay
renewal must issue a PAID vendor renew, not a free panel refresh.

The scheduled autopay loop charges the balance and extends the local ``end_date``
(``extend_subscription``), then syncs the desired state. For an Artemida
(external-vendor) subscription that sync MUST route through ``renew_external`` — a PAID
vendor renew keyed on the post-extend ``end_date`` — and NOT ``update_remnawave_user``
(whose external branch is a free ``sync_usage`` refresh that never extends the vendor
key). Otherwise autopay silently bills the client while the vendor key still expires at
the original date and the paying client loses access.

A remnawave subscription must be unaffected: ``renew_external`` returns False and the
existing ``update_remnawave_user`` panel sync runs exactly as before.

There is no pre-existing isolated test for ``_process_autopayments`` (it is a single
large method), so this drives the whole method once with a mocked async session and
patched collaborators — no real network or DB.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.database.models import SubscriptionStatus
from app.services import monitoring_service as mon_mod
from app.services.monitoring_service import MonitoringService


def _make_eligible_subscription():
    now = datetime.now(UTC)
    user = SimpleNamespace(
        id=1,
        telegram_id=123456,  # truthy: email-only branch skipped; bot is None so success-notify is skipped
        balance_kopeks=10_000_000,
        language='ru',
    )
    sub = SimpleNamespace(
        id=42,
        user_id=1,
        user=user,
        status=SubscriptionStatus.ACTIVE.value,
        autopay_enabled=True,
        is_trial=False,
        tariff=None,  # classic sub: skips the daily-tariff and priced-period side checks
        tariff_id=None,
        autopay_period_days=None,  # resolve_autopay_period_candidate is patched anyway
        autopay_days_before=3,
        end_date=now + timedelta(hours=1),  # days_before_expiry == 0 -> eligible
        device_limit=1,
    )
    return sub, user


def _patch_autopay_collaborators(monkeypatch, subscription, user):
    # Classic mode so a tariff-less subscription is not skipped by the tariffs guard.
    monkeypatch.setattr(settings, 'SALES_MODE', 'classic')

    # Period resolution -> fixed 90 (avoids needing a real tariff / priced periods).
    monkeypatch.setattr(mon_mod, 'resolve_autopay_period_candidate', MagicMock(return_value=90))
    monkeypatch.setattr(mon_mod, 'get_user_active_promo_discount_percent', MagicMock(return_value=0))
    monkeypatch.setattr(mon_mod, 'subtract_user_balance', AsyncMock(return_value=True))
    monkeypatch.setattr(mon_mod, 'extend_subscription', AsyncMock(return_value=subscription))

    # Lazily-imported inside the method — patch at their source modules.
    monkeypatch.setattr('app.database.crud.subscription.is_recently_updated_by_webhook', MagicMock(return_value=False))
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr(
        'app.services.pricing_engine.pricing_engine.calculate_renewal_price',
        AsyncMock(return_value=SimpleNamespace(final_total=100)),
    )
    monkeypatch.setattr('app.database.crud.transaction.create_transaction', AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr('app.services.subscription_renewal_service.with_admin_notification_service', AsyncMock())


def _make_db(subscription) -> AsyncMock:
    db = AsyncMock()
    result = MagicMock()
    # The initial eligibility query consumes .scalars().all(); the per-item refetches
    # consume .scalar_one_or_none(). One result object serves every db.execute call.
    result.scalars.return_value.all.return_value = [subscription]
    result.scalar_one_or_none.return_value = subscription
    result.scalar_one.return_value = subscription
    db.execute = AsyncMock(return_value=result)
    return db


def _make_service(renew_external_returns: bool) -> MonitoringService:
    svc = MonitoringService(bot=None)
    svc.subscription_service = MagicMock()
    svc.subscription_service.renew_external = AsyncMock(return_value=renew_external_returns)
    svc.subscription_service.update_remnawave_user = AsyncMock()
    svc.subscription_service.create_remnawave_user = AsyncMock()
    svc._log_monitoring_event = AsyncMock()  # avoid an extra db round trip at loop end
    return svc


@pytest.mark.asyncio
async def test_autopay_artemida_renews_vendor_not_panel(monkeypatch):
    sub, user = _make_eligible_subscription()
    _patch_autopay_collaborators(monkeypatch, sub, user)
    svc = _make_service(renew_external_returns=True)
    db = _make_db(sub)

    await svc._process_autopayments(db)

    svc.subscription_service.renew_external.assert_awaited_once()
    assert svc.subscription_service.renew_external.await_args.kwargs['period_days'] == 90
    # PAID vendor renew handled it — the free panel refresh must NOT run.
    svc.subscription_service.update_remnawave_user.assert_not_awaited()
    svc.subscription_service.create_remnawave_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_autopay_remnawave_uses_panel_update(monkeypatch):
    sub, user = _make_eligible_subscription()
    _patch_autopay_collaborators(monkeypatch, sub, user)
    svc = _make_service(renew_external_returns=False)
    db = _make_db(sub)

    await svc._process_autopayments(db)

    svc.subscription_service.renew_external.assert_awaited_once()
    assert svc.subscription_service.renew_external.await_args.kwargs['period_days'] == 90
    # remnawave: renew_external returned False -> the existing panel sync still runs.
    svc.subscription_service.update_remnawave_user.assert_awaited_once()
