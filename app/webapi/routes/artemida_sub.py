"""MAX-branded, full-tunnel subscription document for Artemida-provisioned subscriptions.

Public route (no API-token dependency) — clients fetch it directly, the same
way a subscription URL from any other provider is fetched. The vendor key id
is stored on ``Subscription.external_ref`` (with ``external_provider ==
'artemida'``); this route resolves it, pulls the raw links from the vendor,
and rebrands them into our own base64 subscription document (see
``app.services.subscription_rebrand``).
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException, Response
from sqlalchemy import select

from app.config import settings
from app.database.database import AsyncSessionLocal
from app.database.models import Subscription
from app.external.artemida_api import ArtemidaAPIError, ArtemidaClient
from app.services.subscription_rebrand import rebrand_links


logger = structlog.get_logger(__name__)
router = APIRouter()


def _make_client() -> ArtemidaClient:
    return ArtemidaClient()


async def _load_subscription_by_ref(external_ref: str) -> Subscription | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Subscription).where(
                Subscription.external_ref == external_ref,
                Subscription.external_provider == 'artemida',
            )
        )
        return result.scalar_one_or_none()


@router.get('/a/{token}')
async def artemida_subscription(token: str) -> Response:
    subscription = await _load_subscription_by_ref(token)
    if subscription is None:
        raise HTTPException(status_code=404, detail='not found')
    try:
        async with _make_client() as client:
            data = await client.get_subscription_links(subscription.external_ref)
    except ArtemidaAPIError as error:
        logger.warning('Artemida subscription-links failed', token=token, error=str(error))
        raise HTTPException(status_code=502, detail='vendor unavailable') from error
    links = list(data.get('links') or [])
    if not links:
        logger.warning('Artemida subscription-links пуст', token=token)
    doc = rebrand_links(
        links,
        title=settings.ARTEMIDA_BRAND_TITLE,
        remark_prefix=(settings.ARTEMIDA_BRAND_TITLE.split() or ['VPN'])[0],
        support_url=settings.ARTEMIDA_BRAND_SUPPORT_URL,
    )
    return Response(content=doc.body, media_type='text/plain', headers=doc.headers)
