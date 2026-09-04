import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock


ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.services import referral_withdrawal_service as module
from app.services.referral_withdrawal_service import referral_withdrawal_service as service


def _stats(available_kopeks=500000):
    return {
        'total_earned': available_kopeks,
        'own_deposits': 0,
        'spending': 0,
        'referral_spent': 0,
        'withdrawn': 0,
        'pending': 0,
        'available_referral': available_kopeks,
        'available_total': available_kopeks,
        'only_referral_mode': True,
    }


async def _ask(monkeypatch, *, is_partner, partners_only=True):
    """Спрашивает сервис, можно ли выводить, при живых деньгах и включённом выводе."""
    monkeypatch.setattr(module.settings, 'REFERRAL_PROGRAM_ENABLED', True)
    monkeypatch.setattr(module.settings, 'REFERRAL_WITHDRAWAL_ENABLED', True)
    monkeypatch.setattr(module.settings, 'REFERRAL_WITHDRAWAL_PARTNERS_ONLY', partners_only)
    monkeypatch.setattr(module.settings, 'REFERRAL_WITHDRAWAL_MIN_AMOUNT_KOPEKS', 100000)
    monkeypatch.setattr(module.settings, 'REFERRAL_WITHDRAWAL_TEST_MODE', False)

    user = SimpleNamespace(id=1, is_partner=is_partner)
    db = SimpleNamespace(get=AsyncMock(return_value=user), execute=AsyncMock())

    monkeypatch.setattr(service, 'get_referral_balance_stats', AsyncMock(return_value=_stats()))
    monkeypatch.setattr(service, 'get_last_withdrawal_request', AsyncMock(return_value=None))

    return await service.can_request_withdrawal(db, user.id)


async def test_non_partner_cannot_withdraw(monkeypatch):
    """Обычный реферер с деньгами на балансе заявку подать не может."""
    can_request, reason, _ = await _ask(monkeypatch, is_partner=False)

    assert can_request is False
    assert 'партнёр' in reason.lower()


async def test_partner_can_withdraw(monkeypatch):
    """Одобренный партнёр проходит проверку."""
    can_request, reason, _ = await _ask(monkeypatch, is_partner=True)

    assert can_request is True, reason


async def test_partners_only_can_be_switched_off(monkeypatch):
    """С выключенным REFERRAL_WITHDRAWAL_PARTNERS_ONLY вывод снова открыт всем."""
    can_request, reason, _ = await _ask(monkeypatch, is_partner=False, partners_only=False)

    assert can_request is True, reason
