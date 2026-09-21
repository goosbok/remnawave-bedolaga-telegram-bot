from __future__ import annotations

from collections.abc import Callable

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import Subscription, SubscriptionStatus, generate_public_token
from app.external.artemida_api import ArtemidaAPIError, ArtemidaClient


logger = structlog.get_logger(__name__)

_MAX_VENDOR_DAYS = 90


def _chunk_days(days: int) -> list[int]:
    """Split a total day count into pieces of at most ``_MAX_VENDOR_DAYS`` days.

    The ARTΞMIDA vendor hard-caps a single create/renew call's period at
    ``_MAX_VENDOR_DAYS`` days (confirmed live: ``days=360`` -> ``invalid_pricing_params``),
    so a longer subscription term must be bought as back-to-back chunks.
    """
    if days < 1:
        raise ValueError(f'days must be >= 1, got {days}')
    chunks: list[int] = []
    remaining = days
    while remaining > 0:
        chunk = min(remaining, _MAX_VENDOR_DAYS)
        chunks.append(chunk)
        remaining -= chunk
    return chunks


async def _maybe_alert_low_balance() -> None:
    """After a vendor spend, ping admins if the balance is low. Best-effort."""
    try:
        from app.services.artemida_balance_alert import check_balance_and_alert

        await check_balance_and_alert()
    except Exception:
        pass


class ArtemidaProvider:
    name = 'artemida'

    def __init__(self, client_factory: Callable[[], ArtemidaClient] | None = None):
        self._client_factory = client_factory or (lambda: ArtemidaClient())

    def build_subscription_url(self, subscription: Subscription) -> str:
        base = settings.ARTEMIDA_REBRAND_BASE_URL.rstrip('/')
        token = getattr(subscription, 'public_token', None)
        return f'{base}/{token}' if base and token else ''

    async def provision(self, *, db: AsyncSession, subscription: Subscription, days: int) -> None:
        """Provision the vendor key for a subscription.

        Precondition: subscription.tariff and subscription.end_date must already be loaded/set.

        The vendor hard-caps a single key's period at ``_MAX_VENDOR_DAYS`` days, so a
        longer term is bought as back-to-back chunks: one ``create_key`` for the first
        chunk, followed by one ``renew_key`` per remaining chunk against that same key.
        """
        if not settings.ARTEMIDA_REBRAND_BASE_URL:
            raise ArtemidaAPIError('ARTEMIDA_REBRAND_BASE_URL is not configured')

        is_trial = bool(getattr(subscription, 'is_trial', False))
        async with self._client_factory() as client:
            if is_trial:
                # A trial is a SEPARATE vendor endpoint (POST /trial) with a
                # vendor-fixed duration and device count. create_key must NOT be
                # used for it: the vendor only accepts the discrete paid periods
                # {7,30,60,90}, so a 1-day trial routed through create_key is
                # rejected as "параметры покупки вне допустимого диапазона".
                key = await client.create_trial(
                    idempotency_key=f'sub-{subscription.id}-trial',
                )
                devices = key.devices or subscription.tariff.device_limit
                n_chunks = 1
            else:
                devices = subscription.tariff.device_limit
                chunks = _chunk_days(days)
                n_chunks = len(chunks)
                key = await client.create_key(
                    days=chunks[0],
                    devices=devices,
                    name=f'sub{subscription.id}',
                    customer_ref=str(subscription.id),
                    idempotency_key=f'sub-{subscription.id}-provision-0',
                )
                chunks_done = 1  # the create_key chunk (chunk 0) already succeeded
                try:
                    for i, chunk in enumerate(chunks[1:], start=1):
                        await client.renew_key(
                            key.id,
                            days=chunk,
                            idempotency_key=f'sub-{subscription.id}-provision-{i}',
                        )
                        chunks_done += 1
                except Exception:
                    # A chunk AFTER the first failed: the vendor key was already created
                    # (and possibly renewed further) before this failure, so the owner has
                    # already paid for chunks_done/len(chunks) chunks of an orphaned,
                    # partial-coverage key — distinct from an ordinary single-call failure,
                    # where nothing was ever charged. Flag it for ops, then propagate as-is
                    # so the caller's rollback path (which does not touch the vendor) runs.
                    logger.warning(
                        'Artemida provision: частичная оплата — создан ключ, но не все чанки продлены',
                        subscription_id=subscription.id,
                        key_id=key.id,
                        chunks_total=len(chunks),
                        chunks_done=chunks_done,
                    )
                    raise
        subscription.external_provider = self.name
        subscription.external_ref = key.id
        subscription.device_limit = devices
        if not subscription.public_token:
            subscription.public_token = generate_public_token()
        subscription.subscription_url = self.build_subscription_url(subscription)
        subscription.status = SubscriptionStatus.ACTIVE.value
        logger.info(
            'Ключ Artemida выдан', subscription_id=subscription.id, key_id=key.id, days=days, chunks=n_chunks
        )
        await _maybe_alert_low_balance()

    async def update(
        self, *, db: AsyncSession, subscription: Subscription, days: int | None = None, devices: int | None = None
    ) -> None:
        if not subscription.external_ref:
            return
        if subscription.end_date is None:
            raise ArtemidaAPIError('subscription.end_date must be set before renew/upgrade')

        async with self._client_factory() as client:
            if days is not None:
                chunks = _chunk_days(days)
                ts = int(subscription.end_date.timestamp())
                for i, chunk in enumerate(chunks):
                    # devices is sent on EVERY chunk, not just the first: whether the
                    # vendor leaves the device count unchanged when the field is
                    # omitted from a renew call is unverified, so each chunk asserts
                    # the intended device count explicitly rather than relying on that.
                    await client.renew_key(
                        subscription.external_ref,
                        days=chunk,
                        devices=devices,
                        idempotency_key=f'sub-{subscription.id}-renew-{ts}-{i}',
                    )
                if devices is not None:
                    subscription.device_limit = devices
                logger.info(
                    'Ключ Artemida продлён',
                    subscription_id=subscription.id,
                    days=days,
                    devices=devices,
                    chunks=len(chunks),
                )
            elif devices is not None:
                await client.upgrade_key(
                    subscription.external_ref,
                    devices=devices,
                    idempotency_key=(
                        f'sub-{subscription.id}-upgrade-{devices}-{int(subscription.end_date.timestamp())}'
                    ),
                )
                subscription.device_limit = devices
                logger.info('Ключ Artemida обновлён (устройства)', subscription_id=subscription.id, devices=devices)
        if days is not None or devices is not None:
            await _maybe_alert_low_balance()

    async def revoke(self, *, db: AsyncSession, subscription: Subscription) -> None:
        if not subscription.external_ref:
            return
        async with self._client_factory() as client:
            await client.revoke_key(
                subscription.external_ref,
                idempotency_key=f'sub-{subscription.id}-revoke',
            )
        logger.info('Ключ Artemida отозван', subscription_id=subscription.id, key_id=subscription.external_ref)

    async def sync_usage(self, *, db: AsyncSession, subscription: Subscription) -> None:
        if not subscription.external_ref:
            return
        async with self._client_factory() as client:
            key = await client.get_key(subscription.external_ref)
        if key.subscription_url:
            if not subscription.public_token:
                subscription.public_token = generate_public_token()
            subscription.subscription_url = self.build_subscription_url(subscription)
        if key.devices is not None:
            subscription.device_limit = key.devices
        if key.status is not None and key.status != 'ACTIVE':
            logger.warning('Дрейф статуса ключа Artemida', subscription_id=subscription.id, vendor_status=key.status)
        logger.info('Синхронизация ключа Artemida', subscription_id=subscription.id, key_id=subscription.external_ref)

    async def fetch_links(self, subscription: Subscription) -> list[str]:
        if not subscription.external_ref:
            return []
        async with self._client_factory() as client:
            data = await client.get_subscription_links(subscription.external_ref)
        return list(data.get('links') or [])

    def _rebrand_headers(self, vendor_headers) -> dict[str, str]:
        """Swap the vendor's brand headers for ours; drop its web page/announce.

        Node names inside the body (country remarks) are the vendor's and kept
        as-is — only the brand surface (title/support) is ours. The vendor's
        ``profile-web-page-url`` and ``announce`` are dropped so its domain/brand
        never reach the client.
        """
        import base64

        out: dict[str, str] = {}
        title = (settings.ARTEMIDA_BRAND_TITLE or 'VPN').strip()
        out['profile-title'] = 'base64:' + base64.b64encode(title.encode()).decode()
        out['profile-update-interval'] = vendor_headers.get('profile-update-interval', '12') or '12'
        support = (settings.ARTEMIDA_BRAND_SUPPORT_URL or '').strip()
        if support:
            out['support-url'] = support
        # Our info block (announce) instead of the vendor's — `\n` in the config
        # value is a literal backslash-n from .env, so unescape it to real newlines.
        announce = (settings.ARTEMIDA_BRAND_ANNOUNCE or '').replace('\\n', '\n').strip()
        if announce:
            out['announce'] = 'base64:' + base64.b64encode(announce.encode()).decode()
        return out

    async def fetch_subscription(
        self, subscription: Subscription, *, client_headers: dict[str, str]
    ) -> tuple[bytes, str, dict[str, str]]:
        """Fetch the vendor's REAL subscription document under our branding.

        The vendor gates real nodes behind an ``x-hwid`` device-binding header:
        without it the smart subscription URL only returns a "use the recommended
        app / enable HWID" placeholder (``0.0.0.0:1``). We forward the client's
        own ``x-hwid`` + ``User-Agent`` so the vendor returns real nodes AND counts
        devices correctly, then rewrite the brand headers to ours.

        Returns ``(body, content_type, rebranded_headers)``.
        """
        if not subscription.external_ref:
            return b'', 'text/plain; charset=utf-8', {}
        async with self._client_factory() as client:
            key = await client.get_key(subscription.external_ref)
        vendor_url = key.subscription_url
        if not vendor_url:
            return b'', 'text/plain; charset=utf-8', {}
        forwarded = {
            name: client_headers[name]
            for name in ('user-agent', 'x-hwid', 'accept')
            if client_headers.get(name)
        }
        body, content_type, vendor_headers = await self._fetch_vendor_document(vendor_url, forwarded)
        return body, content_type, self._rebrand_headers(vendor_headers)

    async def _fetch_vendor_document(
        self, url: str, headers: dict[str, str]
    ) -> tuple[bytes, str, dict[str, str]]:
        """HTTP seam for the vendor's smart subscription URL (patched in tests)."""
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, allow_redirects=True) as resp:
                body = await resp.read()
                content_type = resp.headers.get('content-type', 'text/plain; charset=utf-8')
                return body, content_type, dict(resp.headers)
