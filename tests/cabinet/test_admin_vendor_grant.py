"""Admin grant of a vendor-backed tariff must BUY the key at the vendor.

Regression guard for the gap where the cabinet admin "create subscription"
action provisioned every tariff on our own Remnawave panel: for a
vendor-backed (Artemida) tariff the client got a panel user with squads that
mean nothing for the vendor key, while the vendor key was never purchased.

Covered here:
- create on a vendor tariff provisions via the provider, skips the panel sync;
- a failed vendor purchase rolls the grant back (row deleted / revive reverted);
- a revive of an already-provisioned vendor sub RENEWS the key instead of
  buying a second one;
- extend on a vendor sub is a paid vendor renew with local compensation;
- the admin-only mutations that cannot work for a vendor key are rejected.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.cabinet.routes import admin_users
from app.cabinet.schemas.users import UpdateSubscriptionRequest


OWNER_ID = 10
VENDOR_TARIFF_ID = 7
PANEL_TARIFF_ID = 3


def _tariff(*, tariff_id: int = VENDOR_TARIFF_ID, provider: str = 'artemida') -> SimpleNamespace:
    return SimpleNamespace(
        id=tariff_id,
        name='Максимум',
        provider=provider,
        traffic_limit_gb=0,
        device_limit=3,
        allowed_squads=['squad-uuid-1'],
        is_daily=False,
    )


def _subscription(**overrides) -> SimpleNamespace:
    base = {
        'id': 55,
        'user_id': OWNER_ID,
        'is_active': True,
        'status': 'active',
        'is_trial': False,
        'start_date': datetime.now(UTC),
        'end_date': datetime.now(UTC) + timedelta(days=30),
        'traffic_limit_gb': 0,
        'traffic_used_gb': 0.0,
        'device_limit': 3,
        'connected_squads': ['squad-uuid-1'],
        'tariff_id': VENDOR_TARIFF_ID,
        'tariff': None,
        'remnawave_id': None,
        'external_provider': 'artemida',
        'external_ref': 'vendor-key-1',
        'is_external_vendor': True,
        'autopay_enabled': False,
        'grace_suppressed_until': None,
        'is_daily_paused': False,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(*subscriptions: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(
        id=OWNER_ID,
        telegram_id=1000,
        remnawave_id=None,
        subscriptions=list(subscriptions),
        promo_group_id=None,
        promo_group=None,
    )


def _admin() -> SimpleNamespace:
    return SimpleNamespace(id=1)


def _db() -> AsyncMock:
    db = AsyncMock()
    return db


class _SubscriptionServiceRecorder:
    """Records which provider seams the route used."""

    instances: list[_SubscriptionServiceRecorder] = []

    def __init__(self):
        self.calls: list[tuple] = []
        self.fail_with: Exception | None = None
        _SubscriptionServiceRecorder.instances.append(self)

    async def _maybe_fail(self):
        if self.fail_with is not None:
            raise self.fail_with

    async def create_remnawave_user(self, db, subscription, **kwargs):
        self.calls.append(('create', subscription.id))
        await self._maybe_fail()
        return object()

    async def renew_external(self, db, subscription, *, period_days):
        self.calls.append(('renew', subscription.id, period_days))
        await self._maybe_fail()
        return True

    async def revoke_external(self, db, subscription):
        self.calls.append(('revoke', subscription.id))
        await self._maybe_fail()
        return True

    async def sync_remnawave_user(self, db, subscription, **kwargs):
        self.calls.append(('sync', subscription.id))
        await self._maybe_fail()
        return object()


@pytest.fixture
def service_recorder(monkeypatch):
    _SubscriptionServiceRecorder.instances = []
    monkeypatch.setattr('app.services.subscription_service.SubscriptionService', _SubscriptionServiceRecorder)
    yield _SubscriptionServiceRecorder
    _SubscriptionServiceRecorder.instances = []


def _recorder() -> _SubscriptionServiceRecorder:
    assert _SubscriptionServiceRecorder.instances, 'SubscriptionService was never constructed'
    return _SubscriptionServiceRecorder.instances[-1]


def _patch_common(monkeypatch, user, tariff):
    monkeypatch.setattr(admin_users, 'get_user_by_id', AsyncMock(return_value=user))
    monkeypatch.setattr(admin_users, 'get_tariff_by_id', AsyncMock(return_value=tariff))
    monkeypatch.setattr(admin_users, '_sync_subscription_to_panel', AsyncMock(return_value={}))
    monkeypatch.setattr(admin_users, '_build_subscription_info_async', AsyncMock(return_value=None))


# === create ===


async def test_create_vendor_tariff_provisions_at_vendor(monkeypatch, service_recorder):
    user = _user()
    tariff = _tariff()
    new_sub = _subscription(external_ref=None, external_provider=None, is_external_vendor=False)
    _patch_common(monkeypatch, user, tariff)
    create = AsyncMock(return_value=new_sub)
    monkeypatch.setattr('app.database.crud.subscription.create_paid_subscription', create)
    panel_sync = admin_users._sync_subscription_to_panel

    response = await admin_users.update_user_subscription(
        OWNER_ID,
        UpdateSubscriptionRequest(action='create', tariff_id=VENDOR_TARIFF_ID, days=30),
        admin=_admin(),
        db=_db(),
    )

    assert response.success is True
    assert 'vendor' in response.message
    assert _recorder().calls == [('create', new_sub.id)], 'the vendor key must be provisioned'
    panel_sync.assert_not_called()
    assert create.await_args.kwargs['connected_squads'] == [], 'vendor sub must not carry local squads'
    assert new_sub.connected_squads == []


async def test_create_vendor_tariff_failure_rolls_back(monkeypatch, service_recorder):
    user = _user()
    tariff = _tariff()
    new_sub = _subscription(external_ref=None, external_provider=None, is_external_vendor=False)
    _patch_common(monkeypatch, user, tariff)
    monkeypatch.setattr('app.database.crud.subscription.create_paid_subscription', AsyncMock(return_value=new_sub))
    db = _db()

    async def fail_create(self, db_, subscription, **kwargs):
        raise RuntimeError('vendor is down')

    monkeypatch.setattr(_SubscriptionServiceRecorder, 'create_remnawave_user', fail_create)

    with pytest.raises(HTTPException) as exc_info:
        await admin_users.update_user_subscription(
            OWNER_ID,
            UpdateSubscriptionRequest(action='create', tariff_id=VENDOR_TARIFF_ID, days=30),
            admin=_admin(),
            db=db,
        )

    assert exc_info.value.status_code == 502
    db.delete.assert_awaited_once_with(new_sub), 'the unprovisioned row must not survive'


async def test_create_vendor_tariff_revive_renews_existing_key(monkeypatch, service_recorder):
    tariff = _tariff()
    old_end = datetime.now(UTC) - timedelta(days=5)
    revived = _subscription(status='expired', end_date=old_end)
    user = _user()
    _patch_common(monkeypatch, user, tariff)
    monkeypatch.setattr(
        admin_users,
        'settings',
        SimpleNamespace(is_multi_tariff_enabled=lambda: True),
    )

    async def by_user_and_tariff(db, user_id, tariff_id, include_inactive=False):
        # The duplicate-active guard calls without include_inactive: the sub is
        # expired, so the guard must see nothing. The revive detector does.
        return revived if include_inactive else None

    monkeypatch.setattr('app.database.crud.subscription.get_subscription_by_user_and_tariff', by_user_and_tariff)
    monkeypatch.setattr('app.database.crud.subscription.create_paid_subscription', AsyncMock(return_value=revived))

    response = await admin_users.update_user_subscription(
        OWNER_ID,
        UpdateSubscriptionRequest(action='create', tariff_id=VENDOR_TARIFF_ID, days=90),
        admin=_admin(),
        db=_db(),
    )

    assert response.success is True
    assert _recorder().calls == [('renew', revived.id, 90)], (
        'a revived vendor sub must renew its existing key, not buy a second one'
    )


async def test_create_panel_tariff_keeps_panel_sync(monkeypatch, service_recorder):
    user = _user()
    tariff = _tariff(tariff_id=PANEL_TARIFF_ID, provider='remnawave')
    new_sub = _subscription(
        tariff_id=PANEL_TARIFF_ID, external_ref=None, external_provider=None, is_external_vendor=False
    )
    _patch_common(monkeypatch, user, tariff)
    monkeypatch.setattr('app.database.crud.subscription.create_paid_subscription', AsyncMock(return_value=new_sub))
    panel_sync = admin_users._sync_subscription_to_panel

    response = await admin_users.update_user_subscription(
        OWNER_ID,
        UpdateSubscriptionRequest(action='create', tariff_id=PANEL_TARIFF_ID, days=30),
        admin=_admin(),
        db=_db(),
    )

    assert response.success is True
    panel_sync.assert_awaited_once()
    assert not _SubscriptionServiceRecorder.instances, 'own-panel tariff must not touch vendor seams'


# === extend ===


async def test_extend_vendor_subscription_renews_key(monkeypatch, service_recorder):
    sub = _subscription()
    user = _user(sub)
    _patch_common(monkeypatch, user, _tariff())
    monkeypatch.setattr(admin_users, 'extend_subscription', AsyncMock(return_value=sub))
    panel_sync = admin_users._sync_subscription_to_panel

    response = await admin_users.update_user_subscription(
        OWNER_ID,
        UpdateSubscriptionRequest(action='extend', days=30),
        admin=_admin(),
        db=_db(),
    )

    assert response.success is True
    assert 'vendor' in response.message
    assert _recorder().calls == [('renew', sub.id, 30)]
    panel_sync.assert_not_called()


async def test_extend_vendor_subscription_failure_compensates(monkeypatch, service_recorder):
    old_end = datetime.now(UTC) + timedelta(days=10)
    sub = _subscription(end_date=old_end)
    user = _user(sub)
    _patch_common(monkeypatch, user, _tariff())

    async def extend(db, subscription, days, **kwargs):
        subscription.end_date = subscription.end_date + timedelta(days=days)
        return subscription

    monkeypatch.setattr(admin_users, 'extend_subscription', extend)

    async def fail_renew(self, db_, subscription, *, period_days):
        raise RuntimeError('vendor renew rejected')

    monkeypatch.setattr(_SubscriptionServiceRecorder, 'renew_external', fail_renew)
    db = _db()

    with pytest.raises(HTTPException) as exc_info:
        await admin_users.update_user_subscription(
            OWNER_ID,
            UpdateSubscriptionRequest(action='extend', days=30),
            admin=_admin(),
            db=db,
        )

    assert exc_info.value.status_code == 502
    assert sub.end_date == old_end, 'the local extension must be rolled back on vendor failure'


# === rejected mutations ===


async def test_shorten_vendor_subscription_rejected(monkeypatch):
    sub = _subscription()
    _patch_common(monkeypatch, _user(sub), _tariff())

    with pytest.raises(HTTPException) as exc_info:
        await admin_users.update_user_subscription(
            OWNER_ID,
            UpdateSubscriptionRequest(action='shorten', days=5),
            admin=_admin(),
            db=_db(),
        )

    assert exc_info.value.status_code == 400


async def test_set_end_date_vendor_subscription_rejected(monkeypatch):
    sub = _subscription()
    _patch_common(monkeypatch, _user(sub), _tariff())

    with pytest.raises(HTTPException) as exc_info:
        await admin_users.update_user_subscription(
            OWNER_ID,
            UpdateSubscriptionRequest(action='set_end_date', end_date=datetime.now(UTC) + timedelta(days=15)),
            admin=_admin(),
            db=_db(),
        )

    assert exc_info.value.status_code == 400


async def test_change_tariff_of_vendor_subscription_rejected(monkeypatch):
    sub = _subscription()
    _patch_common(monkeypatch, _user(sub), _tariff(tariff_id=PANEL_TARIFF_ID, provider='remnawave'))

    with pytest.raises(HTTPException) as exc_info:
        await admin_users.update_user_subscription(
            OWNER_ID,
            UpdateSubscriptionRequest(action='change_tariff', tariff_id=PANEL_TARIFF_ID),
            admin=_admin(),
            db=_db(),
        )

    assert exc_info.value.status_code == 400


async def test_change_to_vendor_tariff_rejected(monkeypatch):
    sub = _subscription(tariff_id=PANEL_TARIFF_ID, external_provider=None, external_ref=None, is_external_vendor=False)
    _patch_common(monkeypatch, _user(sub), _tariff())
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())

    with pytest.raises(HTTPException) as exc_info:
        await admin_users.update_user_subscription(
            OWNER_ID,
            UpdateSubscriptionRequest(action='change_tariff', tariff_id=VENDOR_TARIFF_ID),
            admin=_admin(),
            db=_db(),
        )

    assert exc_info.value.status_code == 400


async def test_enable_autopay_vendor_subscription_rejected(monkeypatch):
    sub = _subscription()
    _patch_common(monkeypatch, _user(sub), _tariff())

    with pytest.raises(HTTPException) as exc_info:
        await admin_users.update_user_subscription(
            OWNER_ID,
            UpdateSubscriptionRequest(action='toggle_autopay', autopay_enabled=True),
            admin=_admin(),
            db=_db(),
        )

    assert exc_info.value.status_code == 400
    assert sub.autopay_enabled is False


async def test_set_traffic_vendor_subscription_rejected(monkeypatch):
    sub = _subscription()
    _patch_common(monkeypatch, _user(sub), _tariff())

    with pytest.raises(HTTPException) as exc_info:
        await admin_users.update_user_subscription(
            OWNER_ID,
            UpdateSubscriptionRequest(action='set_traffic', traffic_limit_gb=50),
            admin=_admin(),
            db=_db(),
        )

    assert exc_info.value.status_code == 400


# === cancel / reset release the vendor key ===


async def test_cancel_vendor_subscription_revokes_key(monkeypatch, service_recorder):
    sub = _subscription()
    _patch_common(monkeypatch, _user(sub), _tariff())
    monkeypatch.setattr('app.services.payment.platega.cancel_platega_recurring_for_subscription_safe', AsyncMock())
    monkeypatch.setattr('app.services.payment.lava.cancel_lava_recurring_for_subscription_safe', AsyncMock())
    panel_sync = admin_users._sync_subscription_to_panel

    response = await admin_users.update_user_subscription(
        OWNER_ID,
        UpdateSubscriptionRequest(action='cancel'),
        admin=_admin(),
        db=_db(),
    )

    assert response.success is True
    assert _recorder().calls == [('revoke', sub.id)], 'the vendor key must be released on cancel'
    panel_sync.assert_not_called()


# === set_device_limit is a paid vendor upgrade ===


async def test_set_device_limit_vendor_upgrades_key(monkeypatch):
    sub = _subscription()
    _patch_common(monkeypatch, _user(sub), _tariff())
    provider = AsyncMock()
    provider.name = 'artemida'

    async def upgrade(*, db, subscription, days=None, devices=None):
        subscription.device_limit = devices

    provider.update = upgrade
    monkeypatch.setattr('app.services.providers.get_provider_by_name', lambda name: provider)
    db = _db()

    response = await admin_users.update_user_subscription(
        OWNER_ID,
        UpdateSubscriptionRequest(action='set_device_limit', device_limit=5),
        admin=_admin(),
        db=db,
    )

    assert response.success is True
    assert sub.device_limit == 5
    db.commit.assert_awaited()
