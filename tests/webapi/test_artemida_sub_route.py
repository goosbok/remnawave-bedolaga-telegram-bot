import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.webapi.routes import artemida_sub


def _subscription(public_token='tok_1', external_provider='artemida', external_ref='key_1'):  # noqa: S107 — test fixture id, not a secret
    return SimpleNamespace(
        id=1,
        public_token=public_token,
        external_provider=external_provider,
        external_ref=external_ref,
    )


def _fake_provider(links=None, side_effect=None):
    provider = SimpleNamespace(name='artemida', fetch_links=AsyncMock())
    if side_effect is not None:
        provider.fetch_links.side_effect = side_effect
    else:
        provider.fetch_links.return_value = links if links is not None else []
    return provider


@pytest.mark.asyncio
async def test_route_returns_rebranded_body(monkeypatch):
    monkeypatch.setattr(artemida_sub, '_load_subscription_by_token', AsyncMock(return_value=_subscription()))
    provider = _fake_provider(links=['vless://a@de.example:443#NL 1'])
    monkeypatch.setattr(artemida_sub, 'get_provider_by_name', lambda name: provider)

    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/tok_1')
    assert r.status_code == 200
    assert base64.b64decode(r.text).decode().endswith('#MAX 1')
    assert r.headers['profile-title'] == 'MAX VPN'
    provider.fetch_links.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_links_returns_200_with_empty_body(monkeypatch):
    monkeypatch.setattr(artemida_sub, '_load_subscription_by_token', AsyncMock(return_value=_subscription()))
    provider = _fake_provider(links=[])
    monkeypatch.setattr(artemida_sub, 'get_provider_by_name', lambda name: provider)

    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/tok_1')
    assert r.status_code == 200
    assert r.text == ''


@pytest.mark.asyncio
async def test_unknown_token_404(monkeypatch):
    monkeypatch.setattr(artemida_sub, '_load_subscription_by_token', AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/nope')
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_subscription_with_unknown_provider_returns_404(monkeypatch):
    monkeypatch.setattr(
        artemida_sub,
        '_load_subscription_by_token',
        AsyncMock(return_value=_subscription(external_provider='some-unregistered-vendor')),
    )
    monkeypatch.setattr(artemida_sub, 'get_provider_by_name', lambda name: None)

    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/tok_1')
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_vendor_error_returns_502(monkeypatch):
    from app.external.artemida_api import ArtemidaGatewayError

    monkeypatch.setattr(artemida_sub, '_load_subscription_by_token', AsyncMock(return_value=_subscription()))
    provider = _fake_provider(side_effect=ArtemidaGatewayError('vendor down', status=502))
    monkeypatch.setattr(artemida_sub, 'get_provider_by_name', lambda name: provider)

    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/tok_1')
    assert r.status_code == 502


@pytest.mark.asyncio
async def test_non_artemida_vendor_error_also_returns_502(monkeypatch):
    """A future vendor's ``fetch_links`` can raise its own exception type, unrelated
    to ``ArtemidaAPIError`` — the route must still report it as a 502 gateway error,
    not let it fall through to FastAPI's generic 500."""

    class _Vendor2Error(Exception):
        pass

    monkeypatch.setattr(
        artemida_sub,
        '_load_subscription_by_token',
        AsyncMock(return_value=_subscription(external_provider='vendor2')),
    )
    provider = _fake_provider(side_effect=_Vendor2Error('vendor2 is down'))
    monkeypatch.setattr(artemida_sub, 'get_provider_by_name', lambda name: provider)

    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/tok_1')
    assert r.status_code == 502
