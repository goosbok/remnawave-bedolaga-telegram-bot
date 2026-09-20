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
