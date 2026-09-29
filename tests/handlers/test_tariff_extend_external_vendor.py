"""Bot tariff extend (``tariff_ext_confirm``): an external-vendor sub must get a PAID vendor renew.

``confirm_tariff_extend`` charges the balance and ``extend_subscription`` moves the
local ``end_date``. For an Artemida (external-vendor) subscription the follow-up sync
used to be ``update_remnawave_user`` — which for such a sub is only a FREE
``sync_usage`` refresh — so the client was charged, the DB said "extended", and the
vendor key still expired at the old date.

The handler now routes through ``SubscriptionRenewalService.renew_external_or_compensate``
(the same seam ``finalize`` uses): a vendor renew on success, full compensation
(revert extension + refund + restore promo) on vendor failure. A remnawave
subscription keeps the original panel create/update path unchanged. Mirrors
``tests/services/test_subscription_renewal_finalize_external.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.handlers.subscription import tariff_purchase as handler_mod
from app.services import subscription_renewal_service as renewal_mod


confirm_tariff_extend = handler_mod.confirm_tariff_extend.__wrapped__

PRICE = 500_00
PERIOD = 30


def _tariff() -> SimpleNamespace:
    return SimpleNamespace(
        id=17,
        name='Премиум',
        is_active=True,
        period_prices={str(PERIOD): PRICE},
        device_limit=3,
        traffic_limit_gb=0,
    )


def _subscription(*, provider: str | None, status: str = 'active', end_date=None, is_trial: bool = False):
    return SimpleNamespace(
        id=231,
        user_id=1,
        tariff_id=17,
        is_trial=is_trial,
        status=status,
        end_date=end_date or datetime.now(UTC) + timedelta(days=5),
        device_limit=3,
        remnawave_id=None if provider else 5,
        external_provider=provider,
        external_ref='ext-1' if provider else None,
    )


def _user(*, promo_percent: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        telegram_id=100,
        language='ru',
        remnawave_id=5,
        balance_kopeks=1000_00,
        promo_offer_discount_percent=promo_percent,
        promo_offer_discount_source='campaign' if promo_percent else None,
        promo_offer_discount_expires_at=datetime.now(UTC) + timedelta(days=3) if promo_percent else None,
    )


def _callback() -> MagicMock:
    callback = MagicMock()
    callback.data = f'tariff_ext_confirm:231:17:{PERIOD}'
    callback.answer = AsyncMock()
    callback.message.edit_text = AsyncMock()
    return callback


async def _fake_subtract(db, u, amount, description, *, consume_promo_offer=False, mark_as_paid_subscription=False):
    u.balance_kopeks -= amount
    if consume_promo_offer:
        u.promo_offer_discount_percent = 0
        u.promo_offer_discount_source = None
        u.promo_offer_discount_expires_at = None
    return True


class _Env(SimpleNamespace):
    pass


def _setup(monkeypatch, *, subscription, user, renew_external, promo_offer_discount: int = 0) -> _Env:
    tariff = _tariff()
    env = _Env(subscription=subscription, user=user, refunded=[])

    async def fake_extend(db, sub, days, **kwargs):
        sub.end_date = sub.end_date + timedelta(days=days)
        sub.status = 'active'
        return sub

    async def fake_add(db, u, amount, description='', *, create_transaction=True, transaction_type=None, **kwargs):
        u.balance_kopeks += amount
        env.refunded.append(amount)
        return True

    monkeypatch.setattr(settings, 'RESET_TRAFFIC_ON_PAYMENT', False)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True)
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs')
    monkeypatch.setattr(handler_mod, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(handler_mod, 'get_subscription_by_id_for_user', AsyncMock(return_value=subscription))
    monkeypatch.setattr('app.database.crud.user.lock_user_for_pricing', AsyncMock(return_value=user))
    monkeypatch.setattr('app.database.crud.user.add_user_balance', fake_add)

    from app.services.pricing_engine import pricing_engine

    monkeypatch.setattr(
        pricing_engine,
        'calculate_tariff_purchase_price',
        AsyncMock(return_value=SimpleNamespace(final_total=PRICE, promo_offer_discount=promo_offer_discount)),
    )
    monkeypatch.setattr(handler_mod, 'subtract_user_balance', _fake_subtract)
    monkeypatch.setattr(handler_mod, 'extend_subscription', fake_extend)

    env.service = MagicMock()
    env.service.renew_external = renew_external
    env.service.create_remnawave_user = AsyncMock()
    env.service.update_remnawave_user = AsyncMock()
    monkeypatch.setattr(handler_mod, 'SubscriptionService', lambda: env.service)
    monkeypatch.setattr(renewal_mod, 'SubscriptionService', lambda: env.service)

    env.create_transaction = AsyncMock()
    monkeypatch.setattr(handler_mod, 'create_transaction', env.create_transaction)
    monkeypatch.setattr(
        handler_mod,
        'AdminNotificationService',
        lambda bot: MagicMock(send_subscription_purchase_notification=AsyncMock()),
    )
    monkeypatch.setattr(handler_mod.user_cart_service, 'delete_subscription_cart', AsyncMock())
    monkeypatch.setattr(handler_mod.user_cart_service, 'delete_user_cart', AsyncMock())

    env.retry_queue = MagicMock()
    monkeypatch.setattr('app.services.remnawave_retry_queue.remnawave_retry_queue', env.retry_queue)

    env.state = MagicMock()
    env.state.clear = AsyncMock()
    env.db = AsyncMock()
    return env


@pytest.mark.asyncio
async def test_extend_artemida_renews_vendor_not_panel(monkeypatch):
    subscription = _subscription(provider='artemida')
    old_end = subscription.end_date
    env = _setup(monkeypatch, subscription=subscription, user=_user(), renew_external=AsyncMock(return_value=True))

    callback = _callback()
    await confirm_tariff_extend(callback, env.user, env.db, env.state)

    env.service.renew_external.assert_awaited_once()
    assert env.service.renew_external.await_args.kwargs['period_days'] == PERIOD
    # The PAID vendor renew handled it — the free panel refresh must NOT run.
    env.service.update_remnawave_user.assert_not_awaited()
    env.service.create_remnawave_user.assert_not_awaited()
    assert subscription.end_date == old_end + timedelta(days=PERIOD)
    assert env.user.balance_kopeks == 1000_00 - PRICE
    assert env.refunded == []
    env.create_transaction.assert_awaited_once()
    env.retry_queue.enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_extend_remnawave_uses_panel_update(monkeypatch):
    subscription = _subscription(provider=None)
    env = _setup(monkeypatch, subscription=subscription, user=_user(), renew_external=AsyncMock(return_value=False))

    await confirm_tariff_extend(_callback(), env.user, env.db, env.state)

    env.service.renew_external.assert_awaited_once()
    env.service.update_remnawave_user.assert_awaited_once()
    env.service.create_remnawave_user.assert_not_awaited()
    env.create_transaction.assert_awaited_once()


@pytest.mark.asyncio
async def test_extend_remnawave_panel_failure_defers_to_retry_queue(monkeypatch):
    """Regression: remnawave panel failure stays best-effort — enqueue, no refund."""
    subscription = _subscription(provider=None)
    env = _setup(monkeypatch, subscription=subscription, user=_user(), renew_external=AsyncMock(return_value=False))
    env.service.update_remnawave_user = AsyncMock(side_effect=RuntimeError('panel 500'))

    await confirm_tariff_extend(_callback(), env.user, env.db, env.state)

    env.retry_queue.enqueue.assert_called_once()
    assert env.refunded == []
    assert env.user.balance_kopeks == 1000_00 - PRICE
    env.create_transaction.assert_awaited_once()


@pytest.mark.asyncio
async def test_extend_artemida_vendor_failure_compensates(monkeypatch):
    """Vendor renew fails: revert end_date + expired status, refund, restore promo,
    no payment transaction, no retry-queue, and the client sees an error — not success."""
    old_end = datetime.now(UTC) - timedelta(days=1)
    subscription = _subscription(provider='artemida', status='expired', end_date=old_end)
    user = _user(promo_percent=30)
    env = _setup(
        monkeypatch,
        subscription=subscription,
        user=user,
        renew_external=AsyncMock(side_effect=RuntimeError('vendor HTTP 502')),
        promo_offer_discount=1500,
    )

    callback = _callback()
    await confirm_tariff_extend(callback, env.user, env.db, env.state)

    env.service.renew_external.assert_awaited_once()
    env.service.update_remnawave_user.assert_not_awaited()
    env.service.create_remnawave_user.assert_not_awaited()
    assert subscription.end_date == old_end
    assert subscription.status == 'expired'
    assert env.refunded == [PRICE]
    assert user.balance_kopeks == 1000_00
    assert user.promo_offer_discount_percent == 30
    assert user.promo_offer_discount_source == 'campaign'
    env.create_transaction.assert_not_awaited()
    env.retry_queue.enqueue.assert_not_called()
    shown = callback.message.edit_text.await_args.args[0]
    assert 'Средства возвращены' in shown


@pytest.mark.asyncio
async def test_extend_artemida_trial_rejected_before_charge(monkeypatch):
    """A vendor trial key cannot be renewed — reject before any charge/extend."""
    subscription = _subscription(provider='artemida', is_trial=True)
    old_end = subscription.end_date
    env = _setup(monkeypatch, subscription=subscription, user=_user(), renew_external=AsyncMock(return_value=True))
    extend_spy = AsyncMock()
    monkeypatch.setattr(handler_mod, 'extend_subscription', extend_spy)

    callback = _callback()
    await confirm_tariff_extend(callback, env.user, env.db, env.state)

    extend_spy.assert_not_awaited()
    env.service.renew_external.assert_not_awaited()
    assert env.user.balance_kopeks == 1000_00
    assert subscription.end_date == old_end
    assert callback.answer.await_args.kwargs.get('show_alert') is True
