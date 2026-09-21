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


def _fake_provider(result=None, side_effect=None):
    provider = SimpleNamespace(name='artemida', fetch_subscription=AsyncMock())
    if side_effect is not None:
        provider.fetch_subscription.side_effect = side_effect
    else:
        provider.fetch_subscription.return_value = (
            result if result is not None else (b'', 'text/plain; charset=utf-8', {})
        )
    return provider


@pytest.mark.asyncio
async def test_route_serves_vendor_body_under_our_brand(monkeypatch):
    monkeypatch.setattr(artemida_sub, '_load_subscription_by_token', AsyncMock(return_value=_subscription()))
    provider = _fake_provider(
        result=(b'[{"remarks":"DE 1"}]', 'application/json; charset=utf-8', {'profile-title': 'base64:TUFYIFZQTg=='})
    )
    monkeypatch.setattr(artemida_sub, 'get_provider_by_name', lambda name: provider)

    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/tok_1', headers={'x-hwid': 'dev-1', 'user-agent': 'Happ/3'})
    assert r.status_code == 200
    # the vendor's real body is passed through untouched (real nodes / country names)
    assert r.content == b'[{"remarks":"DE 1"}]'
    assert r.headers['content-type'].startswith('application/json')
    # ...but the brand header is ours, not the vendor's
    assert r.headers['profile-title'] == 'base64:TUFYIFZQTg=='
    # the client's device binding + UA were forwarded so the vendor unlocks real nodes
    _, kwargs = provider.fetch_subscription.call_args
    assert kwargs['client_headers']['x-hwid'] == 'dev-1'
    assert kwargs['client_headers']['user-agent'] == 'Happ/3'


@pytest.mark.asyncio
async def test_empty_body_returns_200(monkeypatch):
    monkeypatch.setattr(artemida_sub, '_load_subscription_by_token', AsyncMock(return_value=_subscription()))
    provider = _fake_provider(result=(b'', 'text/plain; charset=utf-8', {}))
    monkeypatch.setattr(artemida_sub, 'get_provider_by_name', lambda name: provider)

    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/tok_1')
    assert r.status_code == 200
    assert r.content == b''


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
    """A future vendor's ``fetch_subscription`` can raise its own exception type,
    unrelated to ``ArtemidaAPIError`` — the route must still report it as a 502
    gateway error, not let it fall through to FastAPI's generic 500."""

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


def test_rebrand_headers_inject_brand_support_and_announce(monkeypatch):
    """_rebrand_headers swaps the vendor's brand for ours and injects our info block."""
    import base64

    from app.services.providers.artemida import ArtemidaProvider

    monkeypatch.setattr('app.services.providers.artemida.settings.ARTEMIDA_BRAND_TITLE', 'MAX VPN', raising=False)
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_BRAND_SUPPORT_URL', 'https://t.me/MaxSupport2', raising=False
    )
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_BRAND_ANNOUNCE', 'Строка 1\\nСтрока 2', raising=False
    )
    out = ArtemidaProvider()._rebrand_headers(
        {'profile-title': 'base64:vendor', 'announce': 'base64:vendor', 'support-url': 'https://t.me/ArtemidaSupportBot'}
    )
    assert base64.b64decode(out['profile-title'].removeprefix('base64:')).decode() == 'MAX VPN'
    assert out['support-url'] == 'https://t.me/MaxSupport2'
    # \n from .env is unescaped to a real newline
    assert base64.b64decode(out['announce'].removeprefix('base64:')).decode() == 'Строка 1\nСтрока 2'
