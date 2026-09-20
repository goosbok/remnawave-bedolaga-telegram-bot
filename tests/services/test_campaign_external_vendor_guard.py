"""Money-safety: campaign day/tariff bonuses must not free-extend a vendor sub.

Both campaign grant paths (``_apply_subscription_bonus`` and ``_apply_tariff_bonus``)
extend the best existing subscription in multi-tariff mode. For an external-vendor
(Artemida) subscription that moves the local ``end_date`` without renewing the
vendor key — the paying-nothing client silently loses access at the old vendor
expiry, and honoring it would charge the owner the vendor fee for a giveaway. Such
a subscription is skipped exactly like the other "bonus not applied" branches:
``CampaignBonusResult(success=False)`` with no registration recorded. A plain
remnawave subscription still extends as before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import structlog

from app.config import settings
from app.database.models import Subscription
from app.services.campaign_service import AdvertisingCampaignService


PANEL_TARIFF_ID = 5


def _service() -> AdvertisingCampaignService:
    service = AdvertisingCampaignService()
    # Guard returns before any panel sync for a vendor sub; for the remnawave
    # path the extend does call update_remnawave_user — stub it out.
    service.subscription_service = MagicMock()
    service.subscription_service.update_remnawave_user = AsyncMock()
    return service


def _sub(*, provider: str | None, tariff_id: int | None = PANEL_TARIFF_ID) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=1,
        user_id=10,
        external_provider=provider,
        external_ref='key_1' if provider and provider != 'remnawave' else None,
        tariff_id=tariff_id,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=10),
    )


def _user() -> SimpleNamespace:
    # _format_user_log reads telegram_id / email / id.
    return SimpleNamespace(id=10, telegram_id=1010, email=None)


def _campaign() -> SimpleNamespace:
    return SimpleNamespace(
        id=7,
        subscription_duration_days=30,
        subscription_traffic_gb=100,
        subscription_device_limit=3,
        subscription_squads=[],
        tariff_id=PANEL_TARIFF_ID,
        tariff_duration_days=30,
    )


@pytest.fixture()
def multi_tariff(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: True)


@pytest.fixture()
def patched_crud(monkeypatch):
    """Stub the CRUD/panel seams the grant paths touch; return the mocks of interest."""
    extend = AsyncMock()
    record = AsyncMock(return_value=(object(), True))
    monkeypatch.setattr('app.database.crud.subscription.extend_subscription', extend)
    monkeypatch.setattr(
        'app.database.crud.server_squad.get_effective_tariff_squad_uuids', AsyncMock(return_value=[])
    )
    monkeypatch.setattr('app.services.campaign_service.record_campaign_registration', record)
    tariff = SimpleNamespace(
        id=PANEL_TARIFF_ID, name='Максимум', is_active=True, traffic_limit_gb=100, device_limit=3, allowed_squads=[]
    )
    monkeypatch.setattr('app.services.campaign_service.get_tariff_by_id', AsyncMock(return_value=tariff))
    return SimpleNamespace(extend=extend, record=record)


class TestSubscriptionBonus:
    @pytest.mark.asyncio
    async def test_artemida_subscription_is_not_extended(self, monkeypatch, multi_tariff, patched_crud):
        sub = _sub(provider='artemida')
        monkeypatch.setattr(
            'app.database.crud.subscription.get_active_subscriptions_by_user_id', AsyncMock(return_value=[sub])
        )
        service = _service()

        with structlog.testing.capture_logs() as logs:
            result = await service._apply_subscription_bonus(AsyncMock(), _user(), _campaign())

        assert result.success is False
        patched_crud.extend.assert_not_awaited()
        patched_crud.record.assert_not_awaited()  # registration not recorded on skip
        assert any(entry.get('log_level') == 'warning' and entry.get('subscription_id') == 1 for entry in logs)

    @pytest.mark.asyncio
    async def test_remnawave_subscription_is_extended_as_before(self, monkeypatch, multi_tariff, patched_crud):
        sub = _sub(provider=None)
        monkeypatch.setattr(
            'app.database.crud.subscription.get_active_subscriptions_by_user_id', AsyncMock(return_value=[sub])
        )
        service = _service()

        result = await service._apply_subscription_bonus(AsyncMock(), _user(), _campaign())

        assert result.success is True
        patched_crud.extend.assert_awaited_once()


class TestTariffBonus:
    @pytest.mark.asyncio
    async def test_artemida_subscription_is_not_extended(self, monkeypatch, multi_tariff, patched_crud):
        sub = _sub(provider='artemida')
        monkeypatch.setattr(
            'app.database.crud.subscription.get_active_subscriptions_by_user_id', AsyncMock(return_value=[sub])
        )
        service = _service()

        with structlog.testing.capture_logs() as logs:
            result = await service._apply_tariff_bonus(AsyncMock(), _user(), _campaign())

        assert result.success is False
        patched_crud.extend.assert_not_awaited()
        patched_crud.record.assert_not_awaited()
        assert any(entry.get('log_level') == 'warning' and entry.get('subscription_id') == 1 for entry in logs)

    @pytest.mark.asyncio
    async def test_remnawave_subscription_is_extended_as_before(self, monkeypatch, multi_tariff, patched_crud):
        sub = _sub(provider=None)
        monkeypatch.setattr(
            'app.database.crud.subscription.get_active_subscriptions_by_user_id', AsyncMock(return_value=[sub])
        )
        service = _service()

        result = await service._apply_tariff_bonus(AsyncMock(), _user(), _campaign())

        assert result.success is True
        patched_crud.extend.assert_awaited_once()
