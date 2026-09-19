from __future__ import annotations

import structlog
from sqlalchemy import select

from app.config import settings


logger = structlog.get_logger(__name__)


class UnlimitedTrialNotEligible(Exception):
    """Raised when the user does not currently qualify for the unlimited (Artemida) trial."""


class UnlimitedTrialUnavailable(Exception):
    """Raised when no active Artemida trial tariff could be resolved."""


class UnlimitedTrialActivationError(Exception):
    """Raised when activation failed AND rolling back the local trial subscription also failed.

    In that state a half-created active/is_trial ``Subscription`` row survives, which
    permanently flips ``user.has_used_trial('unlimited')`` to ``True`` with a dead
    vendor link — the user is blocked from ever retrying. This is distinct from the
    ordinary case (vendor failed, rollback succeeded), where the original error is
    re-raised as-is so callers can tell the two apart. The original error is chained
    via ``from error`` for diagnostics.
    """


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
    """Тариф безлимит-триала: по ARTEMIDA_TRIAL_TARIFF_ID, иначе первый
    artemida-тариф с is_trial_available. None если нет.

    Без требования is_active: как и у обычного (get_trial_tariff),
    триальный тариф специально может быть неактивным, чтобы не отображаться
    в списке покупки, но всё равно резолвиться для триала."""
    from app.database.models import Tariff

    if cfg.ARTEMIDA_TRIAL_TARIFF_ID:
        tariff = await db.get(Tariff, cfg.ARTEMIDA_TRIAL_TARIFF_ID)
        return tariff if tariff and tariff.provider == 'artemida' and tariff.is_trial_available else None

    result = await db.execute(
        select(Tariff)
        .where(Tariff.provider == 'artemida', Tariff.is_trial_available.is_(True))
        .order_by(Tariff.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def activate_unlimited_trial(db, user, *, bot=None):
    """Активирует безлимит-триал (Artemida) для пользователя.

    Создаёт 1-дневную триальную подписку и провижинит её у вендора Artemida. Любая
    ошибка на этапе провижининга — вендор отказал (``ArtemidaAPIError``, включая
    ``ArtemidaInsufficientBalance``), либо последующий ``db.commit()`` упал уже
    ПОСЛЕ того, как вендор выдал ключ — откатывает (удаляет) только что созданную
    триальную подписку: клиент ничего не платил за безлимит-триал, поэтому
    возврата средств здесь нет, только отмена подписки.

    Если сам откат тоже не удался, поднимается ``UnlimitedTrialActivationError``
    (исходная ошибка — в ``__cause__``), чтобы вызывающий код мог отличить «вендор
    отказал, откат прошёл» (пробрасывается исходная ошибка как есть) от «вендор
    отказал, и откат тоже не прошёл» (эта отдельная ошибка).

    Предусловие: у ``user`` должна быть заранее (eagerly) загружена связь
    ``subscriptions`` — гейт доступности синхронно читает
    ``user.has_used_trial(...)``, который итерирует ``user.subscriptions``; на
    неподгруженной (lazy) связи это упадёт ``MissingGreenlet``-ом в асинхронном
    контексте (то же предусловие, что у ``User.is_trial_already_used`` — «Требует
    загруженного `subscriptions`»). ``get_user_by_id`` уже грузит её через
    ``selectinload``.
    """
    from app.database.crud.subscription import create_trial_subscription
    from app.services.subscription_service import SubscriptionService
    from app.services.trial_activation_service import rollback_trial_subscription_activation

    if not unlimited_trial_available(user):
        raise UnlimitedTrialNotEligible(
            f'User {getattr(user, "id", "<unknown>")} is not eligible for the unlimited trial'
        )

    tariff = await resolve_unlimited_trial_tariff(db)
    if tariff is None:
        raise UnlimitedTrialUnavailable('No active Artemida trial tariff is configured')

    subscription = await create_trial_subscription(
        db,
        user.id,
        duration_days=1,
        traffic_limit_gb=0,
        device_limit=tariff.device_limit,
        tariff_id=tariff.id,
    )

    try:
        # Broad on purpose: the artemida branch of create_remnawave_user() first
        # spends the vendor key in provider.provision() and only THEN calls
        # db.commit() — a commit failure (or anything else past provisioning)
        # would otherwise bypass rollback and leave a half-created active
        # is_trial subscription behind. A post-charge failure here can leave an
        # orphaned 1-day vendor key; that's bounded (auto-expires in a day) and
        # traceable via provision()'s own success log, so it's not handled here.
        await SubscriptionService().create_remnawave_user(db, subscription)
    except Exception as error:
        logger.warning(
            'Не удалось активировать безлимит-триал — откатываем триальную подписку',
            user_id=user.id,
            subscription_id=subscription.id,
            error=error,
        )
        rollback_success = await rollback_trial_subscription_activation(db, subscription)
        if not rollback_success:
            logger.critical(
                'Откат безлимит-триала не удался',
                subscription_id=subscription.id,
                user_id=user.id,
            )
            raise UnlimitedTrialActivationError(
                f'Failed to roll back unlimited trial subscription {subscription.id} '
                f'for user {user.id} after a failed activation'
            ) from error
        raise

    return subscription
