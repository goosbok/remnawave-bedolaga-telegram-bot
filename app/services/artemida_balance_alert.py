"""Low-balance watchdog for the Artemida vendor account.

After each vendor spend (a paid purchase or a renewal) we read the vendor balance
and, if it has dropped below the configured threshold, ping the admin chat — so the
owner tops up before purchases start failing on ``insufficient_balance``.

Best-effort by design: it must never turn a successful purchase into a failure, so
every path swallows its own errors. A small in-process throttle keeps a persistently
low balance from flooding the admin chat on every purchase.
"""

from __future__ import annotations

import time

import structlog

from app.config import settings
from app.external.artemida_api import ArtemidaClient


logger = structlog.get_logger(__name__)

_last_alert_monotonic = 0.0


async def check_balance_and_alert() -> None:
    """Read the vendor balance and alert admins if it is below the threshold."""
    try:
        if not settings.ARTEMIDA_ENABLED:
            return
        threshold = int(getattr(settings, 'ARTEMIDA_LOW_BALANCE_THRESHOLD', 0) or 0)
        if threshold <= 0:
            return

        async with ArtemidaClient() as client:
            data = await client.get_balance()
        balance = float(data.get('balance') or 0)
        currency = str(data.get('currency') or 'RUB')

        if balance >= threshold:
            return

        interval = max(0, int(getattr(settings, 'ARTEMIDA_LOW_BALANCE_ALERT_INTERVAL_MIN', 0) or 0)) * 60
        global _last_alert_monotonic
        now = time.monotonic()
        if interval and (now - _last_alert_monotonic) < interval:
            logger.info('Низкий баланс Artemida, но отбивка зажата троттлом', balance=balance, threshold=threshold)
            return
        _last_alert_monotonic = now

        await _notify_admins(balance, currency, threshold)
        logger.warning('Отбивка о низком балансе Artemida отправлена', balance=balance, threshold=threshold)
    except Exception as error:  # never break the purchase flow
        logger.debug('Проверка баланса Artemida пропущена', error=str(error))


async def _notify_admins(balance: float, currency: str, threshold: int) -> None:
    from app.bot_factory import create_bot
    from app.services.admin_notification_service import AdminNotificationService

    if not settings.get_admin_notifications_chat_id():
        return
    text = (
        '⚠️ <b>Низкий баланс у вендора ARTΞMIDA</b>\n\n'
        f'Осталось: <b>{balance:.0f} {currency}</b> (порог {threshold} {currency}).\n'
        'Пополни баланс вендора — иначе покупки и продления клиентов начнут падать '
        'с ошибкой «недостаточно средств».'
    )
    bot = create_bot()
    try:
        await AdminNotificationService(bot).send_admin_notification(text)
    finally:
        try:
            await bot.session.close()
        except Exception:
            pass
