from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services.providers import get_provider, get_provider_by_name
from app.services.providers.artemida import ArtemidaProvider


def test_get_provider_by_name_artemida():
    assert get_provider_by_name('artemida').name == 'artemida'


def test_get_provider_by_name_remnawave_and_empty():
    assert get_provider_by_name('remnawave').name == 'remnawave'
    assert get_provider_by_name(None).name == 'remnawave'


def test_get_provider_by_name_unknown_returns_none():
    assert get_provider_by_name('nope') is None


def test_get_provider_by_tariff_uses_provider_field():
    assert get_provider(SimpleNamespace(provider='artemida')).name == 'artemida'
    assert get_provider(SimpleNamespace(provider='remnawave')).name == 'remnawave'


def _ctx(obj):
    class _C:
        async def __aenter__(self_):
            return obj

        async def __aexit__(self_, *a):
            return False

    return _C()


@pytest.mark.asyncio
async def test_artemida_fetch_links_returns_links():
    client = AsyncMock()
    client.get_subscription_links.return_value = {'links': ['vless://a@de.example:443#NL 1'], 'count': 1}
    provider = ArtemidaProvider(client_factory=lambda: _ctx(client))
    sub = SimpleNamespace(id=1, external_ref='key_1')
    links = await provider.fetch_links(sub)
    assert links == ['vless://a@de.example:443#NL 1']
    client.get_subscription_links.assert_awaited_once_with('key_1')
