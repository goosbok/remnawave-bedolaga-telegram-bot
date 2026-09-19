"""MAX-branded, full-tunnel subscription document for provider-provisioned subscriptions.

Public route (no API-token dependency) — clients fetch it directly, the same
way a subscription URL from any other provider is fetched. The link is keyed
by ``Subscription.public_token`` (our own stable id, independent of the
vendor), so a subscription can be swapped to a different provider without
breaking the client's saved link. This route resolves the subscription by
that token, dispatches to its *current* provider's ``fetch_links``, and
rebrands the result into our own base64 subscription document (see
``app.services.subscription_rebrand``).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription
from app.external.artemida_api import ArtemidaAPIError
from app.services.providers import get_provider_by_name
from app.services.subscription_rebrand import rebrand_links


logger = structlog.get_logger(__name__)
router = APIRouter()


async def _load_subscription_by_token(public_token: str) -> Subscription | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Subscription).where(Subscription.public_token == public_token))
        return result.scalar_one_or_none()


@router.get('/a/{token}')
async def artemida_subscription(token: str) -> Response:
    subscription = await _load_subscription_by_token(token)
    if subscription is None:
        raise HTTPException(status_code=404, detail='not found')
    provider = get_provider_by_name(subscription.external_provider)
    if provider is None or provider.name == 'remnawave':
        raise HTTPException(status_code=404, detail='not found')
    try:
        links = await provider.fetch_links(subscription)
    except ArtemidaAPIError as error:
        logger.warning('rebrand fetch_links failed', token=token, error=str(error))
        raise HTTPException(status_code=502, detail='vendor unavailable') from error
    if not links:
        logger.warning('rebrand links пусты', token=token)
    doc = rebrand_links(
        list(links),
        title=settings.ARTEMIDA_BRAND_TITLE,
        remark_prefix=(settings.ARTEMIDA_BRAND_TITLE.split() or ['VPN'])[0],
        support_url=settings.ARTEMIDA_BRAND_SUPPORT_URL,
    )
    return Response(content=doc.body, media_type='text/plain', headers=doc.headers)
