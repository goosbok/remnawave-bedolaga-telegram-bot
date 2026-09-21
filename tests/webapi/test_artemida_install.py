from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.webapi.routes import artemida_install, artemida_sub


def test_wants_install_page_distinguishes_browser_from_client():
    assert artemida_install.wants_install_page({'user-agent': 'Mozilla/5.0 (iPhone) AppleWebKit Safari'}) is True
    assert artemida_install.wants_install_page({'user-agent': 'Mozilla/5.0 Chrome/120'}) is True
    # A VPN client is never a browser, even without x-hwid.
    assert artemida_install.wants_install_page({'user-agent': 'Happ/3.26.3'}) is False
    assert artemida_install.wants_install_page({'user-agent': 'v2rayNG/1.8'}) is False
    # x-hwid means it's a client fetching config, regardless of UA.
    assert artemida_install.wants_install_page({'user-agent': 'Mozilla/5.0', 'x-hwid': 'dev1'}) is False
    assert artemida_install.wants_install_page({}) is False


@pytest.mark.asyncio
async def test_render_install_page_uses_appconfig_deeplinks(monkeypatch):
    cfg = {
        'platforms': {
            'ios': {'apps': [{'name': 'INCY', 'blocks': [{'buttons': [{'type': 'external', 'link': 'https://store/incy'}]}]}]},
            'android': {'apps': [{'name': 'Happ', 'blocks': []}]},
        }
    }
    monkeypatch.setattr(
        'app.cabinet.routes.subscription_modules.status._load_app_config_async', AsyncMock(return_value=cfg)
    )
    monkeypatch.setattr(
        'app.cabinet.routes.subscription_modules.status._create_deep_link',
        lambda app, url, crypto: f'happ://add/{url}' if app.get('name') == 'Happ' else 'incy://crypt1/XYZ',
    )
    sub = SimpleNamespace(status='active', end_date=None, subscription_url='https://sub.max/a/tok')
    page = await artemida_install.render_install_page(
        sub, sub_url='https://sub.max/a/tok', brand_title='MAX VPN', support_url='https://t.me/MaxSupport2'
    )
    assert '<html' in page.lower()
    assert 'MAX VPN' in page
    assert 'INCY' in page and 'Happ' in page
    assert 'incy://crypt1/XYZ' in page
    assert 'happ://add/https://sub.max/a/tok' in page
    assert 'https://t.me/MaxSupport2' in page


@pytest.mark.asyncio
async def test_route_browser_gets_install_page_no_vendor_fetch(monkeypatch):
    sub = SimpleNamespace(
        id=1, public_token='tok', external_provider='artemida', external_ref='k', subscription_url='https://sub.max/a/tok'
    )
    monkeypatch.setattr(artemida_sub, '_load_subscription_by_token', AsyncMock(return_value=sub))
    provider = SimpleNamespace(name='artemida', fetch_subscription=AsyncMock())
    monkeypatch.setattr(artemida_sub, 'get_provider_by_name', lambda name: provider)
    monkeypatch.setattr(artemida_sub, 'render_install_page', AsyncMock(return_value='<html>PAGE</html>'))

    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/tok', headers={'user-agent': 'Mozilla/5.0 Chrome/120'})
    assert r.status_code == 200
    assert r.headers['content-type'].startswith('text/html')
    assert r.text == '<html>PAGE</html>'
    provider.fetch_subscription.assert_not_awaited()  # browser must NOT hit the vendor


@pytest.mark.asyncio
async def test_route_client_gets_config_not_page(monkeypatch):
    sub = SimpleNamespace(
        id=1, public_token='tok', external_provider='artemida', external_ref='k', subscription_url='https://sub.max/a/tok'
    )
    monkeypatch.setattr(artemida_sub, '_load_subscription_by_token', AsyncMock(return_value=sub))
    provider = SimpleNamespace(
        name='artemida',
        fetch_subscription=AsyncMock(return_value=(b'CONFIG', 'application/json', {'profile-title': 'base64:x'})),
    )
    monkeypatch.setattr(artemida_sub, 'get_provider_by_name', lambda name: provider)

    app = FastAPI()
    app.include_router(artemida_sub.router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
        r = await ac.get('/a/tok', headers={'user-agent': 'Happ/3.26.3', 'x-hwid': 'dev1'})
    assert r.status_code == 200
    assert r.content == b'CONFIG'
    provider.fetch_subscription.assert_awaited_once()
