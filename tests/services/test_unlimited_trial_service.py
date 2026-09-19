from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.unlimited_trial_service import (
    is_account_verified,
    resolve_unlimited_trial_tariff,
    unlimited_trial_available,
)


def _cfg(**overrides):
    base = dict(ARTEMIDA_ENABLED=True, ARTEMIDA_TRIAL_ENABLED=True, ARTEMIDA_TRIAL_TARIFF_ID=0)
    base.update(overrides)
    return SimpleNamespace(**base)


def _user(*, used=False, email_verified=True, telegram_id=None):
    return SimpleNamespace(
        email_verified=email_verified,
        telegram_id=telegram_id,
        has_used_trial=lambda kind: used,
    )


def _tariff(**overrides):
    base = dict(id=5, provider='artemida', is_trial_available=True, is_active=True)
    base.update(overrides)
    return SimpleNamespace(**base)


class TestIsAccountVerified:
    def test_true_when_email_verified(self):
        user = SimpleNamespace(email_verified=True, telegram_id=None)
        assert is_account_verified(user) is True

    def test_true_when_telegram_id_present(self):
        user = SimpleNamespace(email_verified=False, telegram_id=123)
        assert is_account_verified(user) is True

    def test_false_when_neither(self):
        user = SimpleNamespace(email_verified=False, telegram_id=None)
        assert is_account_verified(user) is False


class TestUnlimitedTrialAvailable:
    def test_true_when_all_conditions_met(self):
        user = _user(used=False, email_verified=True, telegram_id=None)
        assert unlimited_trial_available(user, cfg=_cfg()) is True

    def test_false_when_artemida_disabled(self):
        user = _user(used=False, email_verified=True)
        assert unlimited_trial_available(user, cfg=_cfg(ARTEMIDA_ENABLED=False)) is False

    def test_false_when_trial_disabled(self):
        user = _user(used=False, email_verified=True)
        assert unlimited_trial_available(user, cfg=_cfg(ARTEMIDA_TRIAL_ENABLED=False)) is False

    def test_false_when_account_not_verified(self):
        user = _user(used=False, email_verified=False, telegram_id=None)
        assert unlimited_trial_available(user, cfg=_cfg()) is False

    def test_false_when_trial_already_used(self):
        user = _user(used=True, email_verified=True)
        assert unlimited_trial_available(user, cfg=_cfg()) is False


class TestResolveUnlimitedTrialTariff:
    @pytest.mark.asyncio
    async def test_returns_tariff_by_explicit_id(self):
        db = AsyncMock()
        db.get.return_value = _tariff(id=5)
        cfg = _cfg(ARTEMIDA_TRIAL_TARIFF_ID=5)

        result = await resolve_unlimited_trial_tariff(db, cfg=cfg)

        assert result is not None
        assert result.id == 5
        db.get.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_returns_none_when_explicit_id_is_remnawave_tariff(self):
        db = AsyncMock()
        db.get.return_value = _tariff(provider='remnawave')
        cfg = _cfg(ARTEMIDA_TRIAL_TARIFF_ID=5)

        result = await resolve_unlimited_trial_tariff(db, cfg=cfg)

        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_when_explicit_id_not_found(self):
        db = AsyncMock()
        db.get.return_value = None
        cfg = _cfg(ARTEMIDA_TRIAL_TARIFF_ID=5)

        result = await resolve_unlimited_trial_tariff(db, cfg=cfg)

        assert result is None
