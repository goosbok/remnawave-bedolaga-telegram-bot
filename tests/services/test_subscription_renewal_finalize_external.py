"""Renewal-finalize seam: an external-vendor renewal must issue a PAID vendor renew.

``SubscriptionRenewalService.finalize`` charges the balance and extends the local
``end_date``, then syncs the desired state. For an Artemida (external-vendor)
subscription that sync MUST route through ``renew_external`` — a PAID vendor renew
keyed on the post-extend ``end_date`` — and NOT the ``create_remnawave_user`` /
``update_remnawave_user`` panel path. ``create`` re-provisions with a stable
idempotency key (vendor returns the cached original key) and ``update`` only does a
free ``sync_usage`` refresh; neither extends the vendor key, so the paying client is
charged and the DB says "extended" while the vendor key still expires at the old date.

A remnawave subscription must be completely unaffected: ``renew_external`` returns
False and the original create/update panel path runs exactly as before.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.services import subscription_renewal_service as renewal_mod
from app.services.subscription_renewal_service import SubscriptionRenewalService


def _pricing(period_days: int = 90) -> SimpleNamespace:
    # Deliberately NOT a SubscriptionRenewalPricing instance, so finalize's else-branch
    # reads promo_offer_discount / breakdown. final_total=0 skips the balance charge and
    # the empty breakdown skips add_subscription_servers — keeps the test focused on the
    # panel-sync seam.
    return SimpleNamespace(final_total=0, period_days=period_days, promo_offer_discount=0, breakdown={})


def _make_db(subscription_before: SimpleNamespace) -> AsyncMock:
    db = AsyncMock()
    lock_result = MagicMock()
    lock_result.scalar_one.return_value = subscription_before
    db.execute = AsyncMock(return_value=lock_result)
    return db


def _make_user(remnawave_id) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        remnawave_id=remnawave_id,
        balance_kopeks=0,
        promo_offer_discount_percent=0,
        promo_offer_discount_source=None,
        promo_offer_discount_expires_at=None,
    )


def _patch_common(monkeypatch, service_mock, subscription_after) -> None:
    monkeypatch.setattr(settings, 'RESET_DEVICES_ON_RENEWAL', False)
    monkeypatch.setattr(renewal_mod, 'SubscriptionService', lambda: service_mock)
    monkeypatch.setattr(renewal_mod, 'extend_subscription', AsyncMock(return_value=subscription_after))
    monkeypatch.setattr(renewal_mod, 'create_transaction', AsyncMock(return_value=MagicMock()))
    monkeypatch.setattr(renewal_mod, 'with_admin_notification_service', AsyncMock())


@pytest.mark.asyncio
async def test_finalize_artemida_renews_vendor_not_panel(monkeypatch):
    now = datetime.now(UTC)
    subscription_before = SimpleNamespace(id=42, status='active', end_date=now + timedelta(days=5), remnawave_id=None)
    subscription_after = SimpleNamespace(id=42, end_date=now + timedelta(days=95), remnawave_id=None)

    service_mock = MagicMock()
    service_mock.renew_external = AsyncMock(return_value=True)
    service_mock.create_remnawave_user = AsyncMock()
    service_mock.update_remnawave_user = AsyncMock()
    _patch_common(monkeypatch, service_mock, subscription_after)

    db = _make_db(subscription_before)
    result = await SubscriptionRenewalService().finalize(db, _make_user(None), subscription_before, _pricing(90))

    service_mock.renew_external.assert_awaited_once()
    assert service_mock.renew_external.await_args.kwargs['period_days'] == 90
    # PAID vendor renew handled it — the panel create/update path must NOT run.
    service_mock.create_remnawave_user.assert_not_awaited()
    service_mock.update_remnawave_user.assert_not_awaited()
    assert result.subscription is subscription_after


@pytest.mark.asyncio
async def test_finalize_remnawave_uses_panel_update(monkeypatch):
    now = datetime.now(UTC)
    subscription_before = SimpleNamespace(id=7, status='active', end_date=now + timedelta(days=5), remnawave_id=5)
    subscription_after = SimpleNamespace(id=7, end_date=now + timedelta(days=95), remnawave_id=5)

    service_mock = MagicMock()
    service_mock.renew_external = AsyncMock(return_value=False)
    service_mock.create_remnawave_user = AsyncMock()
    service_mock.update_remnawave_user = AsyncMock()
    _patch_common(monkeypatch, service_mock, subscription_after)

    db = _make_db(subscription_before)
    # Both panel ids set -> the existing decision resolves to update (not create),
    # in either multi-tariff or classic mode.
    result = await SubscriptionRenewalService().finalize(db, _make_user(5), subscription_before, _pricing(90))

    service_mock.renew_external.assert_awaited_once()
    assert service_mock.renew_external.await_args.kwargs['period_days'] == 90
    service_mock.update_remnawave_user.assert_awaited_once()
    service_mock.create_remnawave_user.assert_not_awaited()
    assert result.subscription is subscription_after


async def _fake_subtract(db, u, amount, description, *, consume_promo_offer=False, mark_as_paid_subscription=False):
    # Mirror subtract_user_balance: debit the balance and, when asked, consume the
    # promo offer (zero the fields) so compensation has something to restore.
    u.balance_kopeks -= amount
    if consume_promo_offer:
        u.promo_offer_discount_percent = 0
        u.promo_offer_discount_source = None
        u.promo_offer_discount_expires_at = None
    return True


@pytest.mark.asyncio
async def test_finalize_external_vendor_failure_compensates_and_raises(monkeypatch):
    """A PAID external renew that fails at the vendor must be atomic: revert the
    committed extension, refund the charge, restore the consumed promo offer, and
    NOT enqueue remnawave_retry_queue (which cannot heal an external sub — it routes
    to provision → cached original key → no renewal). The caller must see a clean
    SubscriptionRenewalChargeError, not a false "renewed" success with a lost charge.
    """
    now = datetime.now(UTC)
    old_end = now - timedelta(days=1)  # expired before renewal
    subscription_before = SimpleNamespace(
        id=42, status='expired', end_date=old_end, remnawave_id=None,
        external_provider='artemida', external_ref='ext-1', user_id=1,
    )
    subscription_after = SimpleNamespace(
        id=42, status='active', end_date=now + timedelta(days=89), remnawave_id=None,
        external_provider='artemida', external_ref='ext-1', user_id=1,
    )

    user = SimpleNamespace(
        id=1, remnawave_id=None, balance_kopeks=100_00,
        promo_offer_discount_percent=30, promo_offer_discount_source='campaign',
        promo_offer_discount_expires_at=now + timedelta(days=3),
    )
    initial_balance = user.balance_kopeks

    refunded_amounts: list[int] = []

    async def fake_add(db, u, amount, description='', *, create_transaction=True, transaction_type=None, **kwargs):
        u.balance_kopeks += amount
        refunded_amounts.append(amount)
        return True

    retry_queue = MagicMock()
    monkeypatch.setattr(renewal_mod, 'subtract_user_balance', _fake_subtract)
    monkeypatch.setattr('app.database.crud.user.add_user_balance', fake_add)
    monkeypatch.setattr('app.services.remnawave_retry_queue.remnawave_retry_queue', retry_queue)

    service_mock = MagicMock()
    service_mock.renew_external = AsyncMock(side_effect=RuntimeError('vendor HTTP 502'))
    service_mock.create_remnawave_user = AsyncMock()
    service_mock.update_remnawave_user = AsyncMock()
    _patch_common(monkeypatch, service_mock, subscription_after)

    # final_total>0 -> a real charge happens; promo_offer_discount>0 -> promo is consumed.
    pricing = SimpleNamespace(final_total=500_00, period_days=90, promo_offer_discount=1500, breakdown={})
    db = _make_db(subscription_before)

    with pytest.raises(renewal_mod.SubscriptionRenewalChargeError):
        await SubscriptionRenewalService().finalize(db, user, subscription_before, pricing)

    # The vendor renew was attempted; the panel create/update path must NOT be a fallback.
    service_mock.renew_external.assert_awaited_once()
    service_mock.create_remnawave_user.assert_not_awaited()
    service_mock.update_remnawave_user.assert_not_awaited()
    # Extension reverted (both end_date and the pre-renewal expired status).
    assert subscription_after.end_date == old_end
    assert subscription_after.status == 'expired'
    # Charge refunded back to the pre-charge balance, and the promo offer restored.
    assert refunded_amounts == [500_00]
    assert user.balance_kopeks == initial_balance
    assert user.promo_offer_discount_percent == 30
    assert user.promo_offer_discount_source == 'campaign'
    # An external failure must NOT be enqueued — the retry queue cannot heal it.
    retry_queue.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_external_vendor_success_keeps_charge_and_extension(monkeypatch):
    """Regression: when renew_external succeeds (True), the renewal stands — no
    compensation runs, the extension holds, the balance stays charged, no refund is
    issued, the retry queue is untouched, and no error is raised.
    """
    now = datetime.now(UTC)
    new_end = now + timedelta(days=95)
    subscription_before = SimpleNamespace(
        id=43, status='active', end_date=now + timedelta(days=5), remnawave_id=None,
        external_provider='artemida', external_ref='ext-2', user_id=2,
    )
    subscription_after = SimpleNamespace(
        id=43, status='active', end_date=new_end, remnawave_id=None,
        external_provider='artemida', external_ref='ext-2', user_id=2,
    )
    user = SimpleNamespace(
        id=2, remnawave_id=None, balance_kopeks=100_00,
        promo_offer_discount_percent=0, promo_offer_discount_source=None, promo_offer_discount_expires_at=None,
    )

    add_spy = AsyncMock(return_value=True)
    retry_queue = MagicMock()
    monkeypatch.setattr(renewal_mod, 'subtract_user_balance', _fake_subtract)
    monkeypatch.setattr('app.database.crud.user.add_user_balance', add_spy)
    monkeypatch.setattr('app.services.remnawave_retry_queue.remnawave_retry_queue', retry_queue)

    service_mock = MagicMock()
    service_mock.renew_external = AsyncMock(return_value=True)
    service_mock.create_remnawave_user = AsyncMock()
    service_mock.update_remnawave_user = AsyncMock()
    _patch_common(monkeypatch, service_mock, subscription_after)

    pricing = SimpleNamespace(final_total=500_00, period_days=90, promo_offer_discount=0, breakdown={})
    db = _make_db(subscription_before)
    result = await SubscriptionRenewalService().finalize(db, user, subscription_before, pricing)

    service_mock.renew_external.assert_awaited_once()
    service_mock.create_remnawave_user.assert_not_awaited()
    service_mock.update_remnawave_user.assert_not_awaited()
    assert result.subscription is subscription_after
    assert subscription_after.end_date == new_end
    assert user.balance_kopeks == 100_00 - 500_00  # charge stands, not refunded
    add_spy.assert_not_awaited()
    retry_queue.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_finalize_remnawave_panel_failure_defers_to_retry_queue(monkeypatch):
    """Regression: the remnawave path is byte-for-byte unchanged. renew_external
    returns False, the panel sync raises, and the failure is best-effort — the retry
    queue is enqueued, the money is NOT refunded, and no error is raised.
    """
    now = datetime.now(UTC)
    subscription_before = SimpleNamespace(
        id=8, status='active', end_date=now + timedelta(days=5), remnawave_id=5, user_id=3,
    )
    subscription_after = SimpleNamespace(
        id=8, status='active', end_date=now + timedelta(days=95), remnawave_id=5, user_id=3,
    )
    user = SimpleNamespace(
        id=3, remnawave_id=5, balance_kopeks=100_00,
        promo_offer_discount_percent=0, promo_offer_discount_source=None, promo_offer_discount_expires_at=None,
    )

    add_spy = AsyncMock(return_value=True)
    retry_queue = MagicMock()
    monkeypatch.setattr(renewal_mod, 'subtract_user_balance', _fake_subtract)
    monkeypatch.setattr('app.database.crud.user.add_user_balance', add_spy)
    monkeypatch.setattr('app.services.remnawave_retry_queue.remnawave_retry_queue', retry_queue)

    service_mock = MagicMock()
    service_mock.renew_external = AsyncMock(return_value=False)  # remnawave sub
    service_mock.create_remnawave_user = AsyncMock()
    service_mock.update_remnawave_user = AsyncMock(side_effect=RuntimeError('panel 500'))
    _patch_common(monkeypatch, service_mock, subscription_after)

    pricing = SimpleNamespace(final_total=500_00, period_days=90, promo_offer_discount=0, breakdown={})
    db = _make_db(subscription_before)

    # Best-effort panel sync: it defers, it does not raise.
    result = await SubscriptionRenewalService().finalize(db, user, subscription_before, pricing)

    service_mock.update_remnawave_user.assert_awaited_once()
    service_mock.create_remnawave_user.assert_not_awaited()
    retry_queue.enqueue.assert_called_once()
    add_spy.assert_not_awaited()  # panel failure is best-effort — money is NOT refunded
    assert user.balance_kopeks == 100_00 - 500_00
    assert result.subscription is subscription_after
