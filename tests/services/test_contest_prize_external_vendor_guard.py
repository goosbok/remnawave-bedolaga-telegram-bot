"""Money-safety: a contest DAYS prize must not free-extend a vendor subscription.

The winner is already recorded before the prize is applied, and the award path
already tolerates "won, but no days granted" (it returns an empty prize string
when the winner has no subscription). For an external-vendor (Artemida)
subscription, ``extend_subscription`` would move the local ``end_date`` without
renewing the vendor key, so we reuse that same skip: log a warning and return an
empty prize string, leaving the win intact. A remnawave subscription is extended
exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import structlog

from app.config import settings
from app.database.models import Subscription
from app.services.contests.attempt_service import ContestAttemptService
from app.services.contests.enums import PrizeType


def _sub(*, provider: str | None) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=1,
        user_id=10,
        external_provider=provider,
        external_ref='key_1' if provider and provider != 'remnawave' else None,
        tariff_id=None,
        start_date=now - timedelta(days=1),
        end_date=now + timedelta(days=10),
    )


def _template() -> SimpleNamespace:
    return SimpleNamespace(prize_type=PrizeType.DAYS.value, prize_value='7')


@pytest.fixture()
def single_tariff(monkeypatch):
    monkeypatch.setattr(type(settings), 'is_multi_tariff_enabled', lambda self: False)


@pytest.mark.asyncio
async def test_artemida_subscription_prize_is_not_extended(monkeypatch, single_tariff):
    sub = _sub(provider='artemida')
    extend = AsyncMock()
    monkeypatch.setattr(
        'app.services.contests.attempt_service.get_subscription_by_user_id', AsyncMock(return_value=sub)
    )
    monkeypatch.setattr('app.services.contests.attempt_service.extend_subscription', extend)
    service = ContestAttemptService()

    with structlog.testing.capture_logs() as logs:
        prize = await service._award_prize(AsyncMock(), 10, _template(), 'ru')

    # Same "won, no days granted" outcome the no-subscription branch already uses.
    assert prize == ''
    extend.assert_not_awaited()
    assert any(entry.get('log_level') == 'warning' and entry.get('subscription_id') == 1 for entry in logs)


@pytest.mark.asyncio
async def test_remnawave_subscription_prize_is_extended_as_before(monkeypatch, single_tariff):
    sub = _sub(provider=None)
    extend = AsyncMock()
    monkeypatch.setattr(
        'app.services.contests.attempt_service.get_subscription_by_user_id', AsyncMock(return_value=sub)
    )
    monkeypatch.setattr('app.services.contests.attempt_service.extend_subscription', extend)
    service = ContestAttemptService()

    prize = await service._award_prize(AsyncMock(), 10, _template(), 'ru')

    assert prize != ''  # a real prize message
    extend.assert_awaited_once()
