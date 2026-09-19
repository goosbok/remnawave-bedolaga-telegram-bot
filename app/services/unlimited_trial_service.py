from __future__ import annotations

import structlog
from sqlalchemy import select

from app.config import settings


logger = structlog.get_logger(__name__)


def is_account_verified(user) -> bool:
    """Верифицированный аккаунт: подтверждённая почта ИЛИ привязанный Telegram."""
    return bool(getattr(user, 'email_verified', False) or getattr(user, 'telegram_id', None))


def unlimited_trial_available(user, *, cfg=settings) -> bool:
    """Безлимит-триал (Artemida) доступен: фичи включены, аккаунт верифицирован,
    безлимит-триал этим пользователем ещё не брался."""
    return bool(
        cfg.ARTEMIDA_ENABLED
        and cfg.ARTEMIDA_TRIAL_ENABLED
        and is_account_verified(user)
        and not user.has_used_trial('unlimited')
    )


async def resolve_unlimited_trial_tariff(db, cfg=settings):
    """Тариф безлимит-триала: по ARTEMIDA_TRIAL_TARIFF_ID, иначе первый активный
    artemida-тариф с is_trial_available. None если нет."""
    from app.database.models import Tariff

    if cfg.ARTEMIDA_TRIAL_TARIFF_ID:
        tariff = await db.get(Tariff, cfg.ARTEMIDA_TRIAL_TARIFF_ID)
        return (
            tariff
            if tariff and tariff.provider == 'artemida' and tariff.is_trial_available and tariff.is_active
            else None
        )

    result = await db.execute(
        select(Tariff)
        .where(Tariff.provider == 'artemida', Tariff.is_trial_available.is_(True), Tariff.is_active.is_(True))
        .order_by(Tariff.id)
        .limit(1)
    )
    return result.scalar_one_or_none()
