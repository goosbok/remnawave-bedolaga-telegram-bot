"""Manual admin trigger: move a subscription to another vendor.

Goes through the real ``require_api_token`` dependency (not the direct-call pattern
used elsewhere in this package) specifically so the unauthorized case is exercised
honestly — a direct handler call bypasses FastAPI's ``Security(...)`` layer entirely
and can't observe a 401/403. The swap itself is mocked at ``move_subscription_to_provider``
(see ``tests/integration/test_provider_swap.py`` for the full, DB-backed swap path).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.external.artemida_api import ArtemidaAPIError
from app.services.provider_swap_service import ProviderSwapError
from app.webapi.dependencies import require_api_token
from app.webapi.routes import subscriptions


def _subscription(
    subscription_id: int = 10,
    external_provider: str = 'vendor2',
    subscription_url: str = 'https://sub.max/a/tok_1',
) -> SimpleNamespace:
    return SimpleNamespace(
        id=subscription_id,
        external_provider=external_provider,
        subscription_url=subscription_url,
    )


def _app_authorized() -> FastAPI:
    app = FastAPI()
    app.include_router(subscriptions.router, prefix='/subscriptions')
    app.dependency_overrides[require_api_token] = lambda: SimpleNamespace(id=1)
    return app


@pytest.mark.asyncio
async def test_move_provider_authorized_calls_swap_service(monkeypatch):
    subscription = _subscription()
    get_mock = AsyncMock(return_value=subscription)
    swap_mock = AsyncMock()
    monkeypatch.setattr(subscriptions, '_get_subscription', get_mock)
    monkeypatch.setattr(subscriptions, 'move_subscription_to_provider', swap_mock)

    app = _app_authorized()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.post(f'/subscriptions/{subscription.id}/move-provider', json={'provider': 'vendor2'})

    assert r.status_code == 200
    body = r.json()
    assert body['external_provider'] == 'vendor2'
    assert body['subscription_url'] == 'https://sub.max/a/tok_1'

    get_mock.assert_awaited_once()
    assert get_mock.await_args.args[1] == subscription.id
    swap_mock.assert_awaited_once()
    call_args = swap_mock.await_args.args
    assert call_args[1] is subscription
    assert call_args[2] == 'vendor2'


@pytest.mark.asyncio
async def test_move_provider_unknown_subscription_404(monkeypatch):
    from fastapi import HTTPException, status

    monkeypatch.setattr(
        subscriptions,
        '_get_subscription',
        AsyncMock(side_effect=HTTPException(status.HTTP_404_NOT_FOUND, 'Subscription not found')),
    )
    swap_mock = AsyncMock()
    monkeypatch.setattr(subscriptions, 'move_subscription_to_provider', swap_mock)

    app = _app_authorized()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.post('/subscriptions/999/move-provider', json={'provider': 'vendor2'})

    assert r.status_code == 404
    swap_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_move_provider_bad_target_returns_400(monkeypatch):
    subscription = _subscription()
    monkeypatch.setattr(subscriptions, '_get_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(
        subscriptions,
        'move_subscription_to_provider',
        AsyncMock(side_effect=ProviderSwapError("Unknown or unsupported target provider: 'nope'")),
    )

    app = _app_authorized()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.post(f'/subscriptions/{subscription.id}/move-provider', json={'provider': 'nope'})

    assert r.status_code == 400


@pytest.mark.asyncio
async def test_move_provider_vendor_failure_returns_502(monkeypatch):
    subscription = _subscription()
    monkeypatch.setattr(subscriptions, '_get_subscription', AsyncMock(return_value=subscription))
    monkeypatch.setattr(
        subscriptions,
        'move_subscription_to_provider',
        AsyncMock(side_effect=ArtemidaAPIError('vendor down')),
    )

    app = _app_authorized()
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.post(f'/subscriptions/{subscription.id}/move-provider', json={'provider': 'artemida'})

    assert r.status_code == 502


@pytest.mark.asyncio
async def test_move_provider_unauthorized_without_token_401():
    """No dependency override here — the real ``require_api_token`` runs and must
    reject the request before the (unmocked, would-hit-a-real-DB) handler body runs."""
    app = FastAPI()
    app.include_router(subscriptions.router, prefix='/subscriptions')

    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.post('/subscriptions/10/move-provider', json={'provider': 'vendor2'})

    assert r.status_code == 401
