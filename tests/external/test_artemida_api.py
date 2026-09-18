import pytest

from app.external.artemida_api import (
    ArtemidaAPIError,
    ArtemidaClient,
    ArtemidaGatewayError,
    ArtemidaInsufficientBalance,
)


class _FakeResp:
    def __init__(self, status, payload=None, headers=None, json_exc=None):
        self.status = status
        self._payload = payload
        self.headers = headers or {}
        self._json_exc = json_exc

    async def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeSession:
    def __init__(self, resp):
        self._resp = resp
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._resp

    async def close(self):
        pass


@pytest.mark.asyncio
async def test_create_key_parses_and_sends_idempotency():
    resp = _FakeResp(
        201,
        {
            'ok': True,
            'data': {
                'key': {'id': 'key_1', 'name': 'api_c', 'devices': 3, 'subscriptionUrl': 'https://x'},
                'charged': 160,
                'balance': 1000,
                'currency': 'RUB',
            },
        },
    )
    session = _FakeSession(resp)
    client = ArtemidaClient(base_url='https://artemida.cc/v1', api_key='artm_live_x', session=session)
    key = await client.create_key(days=30, devices=3, name='c', customer_ref='42', idempotency_key='order-42')
    assert key.id == 'key_1'
    assert key.devices == 3
    method, url, kwargs = session.calls[0]
    assert method == 'POST'
    assert url.endswith('/keys')
    assert kwargs['headers']['Authorization'] == 'Bearer artm_live_x'
    assert kwargs['headers']['Idempotency-Key'] == 'order-42'
    assert kwargs['json'] == {'days': 30, 'devices': 3, 'name': 'c', 'customerRef': '42'}


@pytest.mark.asyncio
async def test_insufficient_balance_maps_to_typed_error():
    resp = _FakeResp(402, {'ok': False, 'error': {'code': 'insufficient_balance', 'message': 'Недостаточно средств.'}})
    client = ArtemidaClient(base_url='https://artemida.cc/v1', api_key='k', session=_FakeSession(resp))
    with pytest.raises(ArtemidaInsufficientBalance):
        await client.create_key(days=30, devices=3, name='c', customer_ref='1', idempotency_key='i')


@pytest.mark.asyncio
async def test_subscription_links_returns_links():
    resp = _FakeResp(
        200,
        {
            'ok': True,
            'data': {
                'keyId': 'key_1',
                'subscriptionUrl': 'https://sub/x',
                'count': 2,
                'links': ['vless://a@de.example:443?type=tcp#Netherlands 1', 'trojan://b@nl.example:443#NL 2'],
            },
        },
    )
    client = ArtemidaClient(base_url='https://artemida.cc/v1', api_key='k', session=_FakeSession(resp))
    data = await client.get_subscription_links('key_1')
    assert data['links'][0].startswith('vless://')
    assert data['count'] == 2


@pytest.mark.asyncio
async def test_generic_error_raises_with_code():
    resp = _FakeResp(409, {'ok': False, 'error': {'code': 'operation_in_progress', 'message': 'busy'}})
    client = ArtemidaClient(base_url='https://artemida.cc/v1', api_key='k', session=_FakeSession(resp))
    with pytest.raises(ArtemidaAPIError) as exc:
        await client.revoke_key('key_1', idempotency_key='r')
    assert exc.value.code == 'operation_in_progress'


@pytest.mark.asyncio
async def test_renew_key_sends_idempotency_and_payload():
    resp = _FakeResp(200, {'ok': True, 'data': {'key': {'id': 'key_1', 'devices': 5}}})
    session = _FakeSession(resp)
    client = ArtemidaClient(base_url='https://artemida.cc/v1', api_key='k', session=session)
    key = await client.renew_key('key_1', days=30, devices=5, idempotency_key='renew-1')
    assert key.id == 'key_1'
    method, url, kwargs = session.calls[0]
    assert method == 'POST'
    assert url.endswith('/keys/key_1/renew')
    assert kwargs['headers']['Idempotency-Key'] == 'renew-1'
    assert kwargs['json'] == {'days': 30, 'devices': 5}


@pytest.mark.asyncio
async def test_upgrade_key_sends_idempotency_and_payload():
    resp = _FakeResp(200, {'ok': True, 'data': {'key': {'id': 'key_1', 'devices': 7}}})
    session = _FakeSession(resp)
    client = ArtemidaClient(base_url='https://artemida.cc/v1', api_key='k', session=session)
    key = await client.upgrade_key('key_1', devices=7, idempotency_key='upgrade-1')
    assert key.devices == 7
    method, url, kwargs = session.calls[0]
    assert method == 'POST'
    assert url.endswith('/keys/key_1/upgrade')
    assert kwargs['headers']['Idempotency-Key'] == 'upgrade-1'
    assert kwargs['json'] == {'devices': 7}


@pytest.mark.asyncio
async def test_revoke_key_sends_idempotency():
    resp = _FakeResp(200, {'ok': True, 'data': {}})
    session = _FakeSession(resp)
    client = ArtemidaClient(base_url='https://artemida.cc/v1', api_key='k', session=session)
    await client.revoke_key('key_1', idempotency_key='revoke-1')
    method, url, kwargs = session.calls[0]
    assert method == 'DELETE'
    assert url.endswith('/keys/key_1')
    assert kwargs['headers']['Idempotency-Key'] == 'revoke-1'


@pytest.mark.asyncio
async def test_create_trial_sends_idempotency_and_payload():
    resp = _FakeResp(200, {'ok': True, 'data': {'key': {'id': 'trial_1'}}})
    session = _FakeSession(resp)
    client = ArtemidaClient(base_url='https://artemida.cc/v1', api_key='k', session=session)
    key = await client.create_trial(idempotency_key='trial-1')
    assert key.id == 'trial_1'
    method, url, kwargs = session.calls[0]
    assert method == 'POST'
    assert url.endswith('/trial')
    assert kwargs['headers']['Idempotency-Key'] == 'trial-1'
    assert kwargs['json'] == {}


@pytest.mark.asyncio
async def test_retry_after_header_is_parsed_on_error():
    resp = _FakeResp(
        429, {'ok': False, 'error': {'code': 'rate_limited', 'message': 'slow down'}}, headers={'Retry-After': '3'}
    )
    client = ArtemidaClient(base_url='https://artemida.cc/v1', api_key='k', session=_FakeSession(resp))
    with pytest.raises(ArtemidaAPIError) as exc:
        await client.get_balance()
    assert exc.value.retry_after == 3.0


@pytest.mark.asyncio
async def test_get_request_does_not_send_idempotency_header():
    resp = _FakeResp(200, {'ok': True, 'data': {'balance': 1000, 'currency': 'RUB'}})
    session = _FakeSession(resp)
    client = ArtemidaClient(base_url='https://artemida.cc/v1', api_key='k', session=session)
    await client.get_balance()
    method, url, kwargs = session.calls[0]
    assert method == 'GET'
    assert 'Idempotency-Key' not in kwargs['headers']


@pytest.mark.asyncio
async def test_unparseable_success_body_raises_gateway_error():
    resp = _FakeResp(200, json_exc=ValueError('not json'))
    client = ArtemidaClient(base_url='https://artemida.cc/v1', api_key='k', session=_FakeSession(resp))
    with pytest.raises(ArtemidaGatewayError):
        await client.get_balance()


@pytest.mark.asyncio
async def test_bare_client_without_session_raises_on_use():
    client = ArtemidaClient(base_url='https://artemida.cc/v1', api_key='k')
    with pytest.raises(ArtemidaAPIError):
        await client.get_balance()
