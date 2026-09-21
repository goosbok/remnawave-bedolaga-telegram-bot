from unittest.mock import AsyncMock

import aiohttp
import pytest

from app.external.artemida_api import ArtemidaClient, ArtemidaGatewayError


class _Raiser:
    async def __aenter__(self):
        raise aiohttp.ServerDisconnectedError()

    async def __aexit__(self, *exc):
        return False


class _OkResp:
    status = 200
    headers: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return {'ok': True, 'data': {'x': 1}}


class _FakeSession:
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.calls = 0

    def request(self, method, url, **kwargs):
        self.calls += 1
        return _Raiser() if self.calls <= self.fail_times else _OkResp()


@pytest.mark.asyncio
async def test_request_retries_transient_disconnect_then_succeeds(monkeypatch):
    monkeypatch.setattr('app.external.artemida_api.asyncio.sleep', AsyncMock())
    session = _FakeSession(fail_times=2)
    client = ArtemidaClient(api_key='k', session=session)
    data = await client._request('POST', '/keys/x/renew', json={'days': 30, 'devices': 3}, idempotency_key='i')
    assert data == {'x': 1}
    assert session.calls == 3  # 2 disconnects + 1 success


@pytest.mark.asyncio
async def test_request_gives_up_as_gateway_error_after_retries(monkeypatch):
    monkeypatch.setattr('app.external.artemida_api.asyncio.sleep', AsyncMock())
    session = _FakeSession(fail_times=99)
    client = ArtemidaClient(api_key='k', session=session)
    with pytest.raises(ArtemidaGatewayError):
        await client._request('GET', '/balance')
    assert session.calls == 3  # bounded number of attempts
