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
from fastapi import APIRouter, HTTPException, Request, Response
from sqlalchemy import select

from app.database.database import AsyncSessionLocal
from app.database.models import Subscription
from app.services.providers import get_provider_by_name


logger = structlog.get_logger(__name__)
router = APIRouter()


async def _load_subscription_by_token(public_token: str) -> Subscription | None:
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Subscription).where(Subscription.public_token == public_token))
        return result.scalar_one_or_none()


@router.get('/a/{token}')
async def artemida_subscription(token: str, request: Request) -> Response:
    subscription = await _load_subscription_by_token(token)
    if subscription is None:
        raise HTTPException(status_code=404, detail='not found')
    provider = get_provider_by_name(subscription.external_provider)
    if provider is None or provider.name == 'remnawave':
        raise HTTPException(status_code=404, detail='not found')
    # Forward the client's own headers so the vendor unlocks real nodes: it gates
    # them behind ``x-hwid`` (device binding) and varies the format by User-Agent.
    client_headers = {key.lower(): value for key, value in request.headers.items()}
    try:
        body, content_type, headers = await provider.fetch_subscription(
            subscription, client_headers=client_headers
        )
    except Exception as error:
        # Any failure to reach the vendor — artemida-specific (ArtemidaAPIError) or
        # a future vendor's own exception type — is a gateway error, not a bug in
        # this route: report 502, never fall through to FastAPI's generic 500.
        logger.warning('rebrand fetch_subscription failed', token=token, error=str(error))
        raise HTTPException(status_code=502, detail='vendor unavailable') from error
    return Response(content=body, media_type=content_type, headers=headers)
