"""Autopay go-live guard (MonitoringService._process_autopayments): the scheduled
autopay loop must SKIP external-vendor (Artemida) subscriptions.

Autopay is disabled for external-vendor subs until the paid vendor renewal is verified
live in production. A run would charge the client's balance in the background and then
renew the vendor key; if the vendor errors mid-run this background loop does not
compensate (no refund+revert), so the safe decision is to not autopay these subs yet.
For such a sub the loop must not charge the balance, not extend the local ``end_date``,
and not touch the vendor (``renew_external``) — it simply skips to the next sub and is
NOT counted as a failure (mirrors the loop's other ineligible/skip ``continue``s).

A remnawave (own-panel) subscription must be unaffected: it is charged and extended, and
because ``renew_external`` returns False the existing ``update_remnawave_user`` panel
sync runs exactly as before (byte-for-byte unchanged).

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


def _make_eligible_subscription(*, sub_id=42, user_id=1, is_external_vendor=False):
    now = datetime.now(UTC)
    user = SimpleNamespace(
        id=user_id,
        telegram_id=123456,  # truthy: email-only branch skipped; bot is None so success-notify is skipped
        balance_kopeks=10_000_000,
        language='ru',
    )
    sub = SimpleNamespace(
        id=sub_id,
        user_id=user_id,
        user=user,
        status=SubscriptionStatus.ACTIVE.value,
        autopay_enabled=True,
        is_trial=False,
        # is_external_vendor mirrors Subscription.is_external_vendor (True for a paid
        # third-party vendor like Artemida, False for the own remnawave panel).
        is_external_vendor=is_external_vendor,
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


def _one_result(subscription) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = subscription
    result.scalar_one.return_value = subscription
    return result


def _make_batch_db(eligible_subs, refetch_results) -> AsyncMock:
    """DB whose db.execute returns, in order: the eligibility query (``.scalars().all()``
    -> eligible_subs) followed by each per-item refetch (``.scalar_one_or_none()``)."""
    db = AsyncMock()
    all_result = MagicMock()
    all_result.scalars.return_value.all.return_value = list(eligible_subs)
    db.execute = AsyncMock(side_effect=[all_result, *refetch_results])
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
async def test_autopay_skips_external_vendor_subscription(monkeypatch):
    """Go-live guard: an external-vendor (Artemida) sub is skipped by the autopay loop —
    no charge, no extend, no vendor call — and it is NOT counted as a failure. The loop
    only logs the run at the end when something was processed or failed, so with the sole
    sub skipped that summary event must NOT fire (proof it was treated as an ineligible
    skip, not a failure)."""
    sub, user = _make_eligible_subscription(is_external_vendor=True)
    _patch_autopay_collaborators(monkeypatch, sub, user)
    svc = _make_service(renew_external_returns=False)
    db = _make_db(sub)

    await svc._process_autopayments(db)

    # No charge, no extend, no vendor/panel state push.
    mon_mod.subtract_user_balance.assert_not_awaited()
    mon_mod.extend_subscription.assert_not_awaited()
    svc.subscription_service.renew_external.assert_not_awaited()
    svc.subscription_service.update_remnawave_user.assert_not_awaited()
    svc.subscription_service.create_remnawave_user.assert_not_awaited()
    # Not counted as a failure: processed_count == failed_count == 0 -> no summary event.
    svc._log_monitoring_event.assert_not_awaited()


@pytest.mark.asyncio
async def test_autopay_skips_vendor_but_processes_remnawave_same_batch(monkeypatch):
    """Skipping a vendor sub must not abort the batch: a remnawave sub queued alongside
    it is still charged and renewed via the existing panel-sync path."""
    vendor_sub, _ = _make_eligible_subscription(sub_id=42, user_id=1, is_external_vendor=True)
    remna_sub, remna_user = _make_eligible_subscription(sub_id=43, user_id=2, is_external_vendor=False)
    _patch_autopay_collaborators(monkeypatch, remna_sub, remna_user)
    svc = _make_service(renew_external_returns=False)
    # execute order: eligibility -> refetch(vendor) -> refetch(remnawave) -> post-charge
    # refetch(remnawave). The vendor sub is skipped before its charge, so it needs no
    # post-charge refetch.
    db = _make_batch_db(
        [vendor_sub, remna_sub],
        [_one_result(vendor_sub), _one_result(remna_sub), _one_result(remna_sub)],
    )

    await svc._process_autopayments(db)

    # Exactly one charge/extend/renew — for the remnawave sub only (vendor was skipped).
    mon_mod.subtract_user_balance.assert_awaited_once()
    mon_mod.extend_subscription.assert_awaited_once()
    svc.subscription_service.renew_external.assert_awaited_once()
    assert svc.subscription_service.renew_external.await_args.kwargs['period_days'] == 90
    svc.subscription_service.update_remnawave_user.assert_awaited_once()


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
