from unittest.mock import AsyncMock

import pytest

from app.services import artemida_balance_alert as mod


class _FakeClient:
    def __init__(self, balance):
        self._balance = balance

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_balance(self):
        return {'balance': self._balance, 'currency': 'RUB'}


def _configure(monkeypatch, *, balance, threshold=2000, interval_min=0, enabled=True):
    monkeypatch.setattr('app.services.artemida_balance_alert.settings.ARTEMIDA_ENABLED', enabled, raising=False)
    monkeypatch.setattr(
        'app.services.artemida_balance_alert.settings.ARTEMIDA_LOW_BALANCE_THRESHOLD', threshold, raising=False
    )
    monkeypatch.setattr(
        'app.services.artemida_balance_alert.settings.ARTEMIDA_LOW_BALANCE_ALERT_INTERVAL_MIN', interval_min, raising=False
    )
    monkeypatch.setattr(mod, 'ArtemidaClient', lambda *a, **k: _FakeClient(balance))
    monkeypatch.setattr(mod, '_last_alert_monotonic', 0.0, raising=False)
    notify = AsyncMock()
    monkeypatch.setattr(mod, '_notify_admins', notify)
    return notify


@pytest.mark.asyncio
async def test_alerts_when_balance_below_threshold(monkeypatch):
    notify = _configure(monkeypatch, balance=1500, threshold=2000)
    await mod.check_balance_and_alert()
    notify.assert_awaited_once()
    args = notify.await_args.args
    assert args[0] == 1500.0 and args[2] == 2000


@pytest.mark.asyncio
async def test_no_alert_when_balance_ok(monkeypatch):
    notify = _configure(monkeypatch, balance=5000, threshold=2000)
    await mod.check_balance_and_alert()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_vendor_skips(monkeypatch):
    notify = _configure(monkeypatch, balance=0, threshold=2000, enabled=False)
    await mod.check_balance_and_alert()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_threshold_zero_disables_check(monkeypatch):
    notify = _configure(monkeypatch, balance=0, threshold=0)
    await mod.check_balance_and_alert()
    notify.assert_not_awaited()


@pytest.mark.asyncio
async def test_throttle_suppresses_second_alert(monkeypatch):
    notify = _configure(monkeypatch, balance=100, threshold=2000, interval_min=60)
    await mod.check_balance_and_alert()
    await mod.check_balance_and_alert()  # within the interval -> throttled
    notify.assert_awaited_once()
