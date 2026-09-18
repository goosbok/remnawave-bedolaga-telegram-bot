import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.webapi.routes import artemida_sub


def _ctx(obj):
    class _C:
        async def __aenter__(self_):
            return obj

        async def __aexit__(self_, *a):
            return False

    return _C()


@pytest.mark.asyncio
async def test_route_returns_rebranded_body(monkeypatch):
    monkeypatch.setattr(
        artemida_sub, '_load_subscription_by_ref', AsyncMock(return_value=SimpleNamespace(id=1, external_ref='key_1'))
    )
    client = AsyncMock()
    client.get_subscription_links.return_value = {'links': ['vless://a@de.example:443#NL 1'], 'count': 1}
    monkeypatch.setattr(artemida_sub, '_make_client', lambda: _ctx(client))

    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/key_1')
    assert r.status_code == 200
    assert base64.b64decode(r.text).decode().endswith('#MAX 1')
    assert r.headers['profile-title'] == 'MAX VPN'


@pytest.mark.asyncio
async def test_empty_links_returns_200_with_empty_body(monkeypatch):
    monkeypatch.setattr(
        artemida_sub, '_load_subscription_by_ref', AsyncMock(return_value=SimpleNamespace(id=1, external_ref='key_1'))
    )
    client = AsyncMock()
    client.get_subscription_links.return_value = {'links': [], 'count': 0}
    monkeypatch.setattr(artemida_sub, '_make_client', lambda: _ctx(client))

    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/key_1')
    assert r.status_code == 200
    assert r.text == ''


@pytest.mark.asyncio
async def test_unknown_token_404(monkeypatch):
    monkeypatch.setattr(artemida_sub, '_load_subscription_by_ref', AsyncMock(return_value=None))
    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/nope')
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_vendor_error_returns_502(monkeypatch):
    from app.external.artemida_api import ArtemidaGatewayError

    monkeypatch.setattr(
        artemida_sub, '_load_subscription_by_ref', AsyncMock(return_value=SimpleNamespace(id=1, external_ref='key_1'))
    )
    client = AsyncMock()
    client.get_subscription_links.side_effect = ArtemidaGatewayError('vendor down', status=502)
    monkeypatch.setattr(artemida_sub, '_make_client', lambda: _ctx(client))
    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/key_1')
    assert r.status_code == 502
