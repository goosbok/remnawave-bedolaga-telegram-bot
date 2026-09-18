"""Клиент ARTΞMIDA vendor /v1 API.

Транспортный слой для интеграции с вендором ARTΞMIDA: создание/продление/
отзыв ключей и получение ссылок подписки. Модель ответа — единый конверт
``{"ok": true, "data": {...}}`` на успехе и ``{"ok": false, "error":
{"code", "message"}}`` на ошибке. Каждый изменяющий состояние вызов несёт
заголовок ``Idempotency-Key``.

HTTP-коды: 402 — недостаточно средств на API-кошельке вендора, 409 —
конфликт идемпотентности/состояния, 429/502/503 — временная ошибка (см.
``Retry-After``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Self

import aiohttp
import structlog

from app.config import settings


logger = structlog.get_logger(__name__)


class ArtemidaAPIError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: str | None = None,
        retry_after: float | None = None,
        data: Any = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.status = status
        self.code = code
        self.retry_after = retry_after
        self.data = data
        self.retryable = retryable


class ArtemidaInsufficientBalance(ArtemidaAPIError):
    """402 — не хватает средств на API-кошельке вендора."""


class ArtemidaGatewayError(ArtemidaAPIError):
    """Ответ без разбираемого конверта, сетевая ошибка или таймаут.

    Платный вызов мог уже примениться на стороне вендора — по умолчанию
    считается безопасным переспросить тем же ``Idempotency-Key``.
    """

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None, data: Any = None):
        super().__init__(message, status=status, code=code, data=data, retryable=True)


@dataclass
class ArtemidaKey:
    id: str | None
    name: str | None = None
    devices: int | None = None
    status: str | None = None
    remaining_days: int | None = None
    expire_at: str | None = None
    subscription_url: str | None = None

    @classmethod
    def from_data(cls, data: dict) -> ArtemidaKey:
        return cls(
            id=data.get('id'),
            name=data.get('name'),
            devices=data.get('devices'),
            status=data.get('status'),
            remaining_days=data.get('remainingDays'),
            expire_at=data.get('expireAt'),
            subscription_url=data.get('subscriptionUrl'),
        )


class ArtemidaClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, *, session: Any = None):
        self.base_url = (base_url or settings.ARTEMIDA_BASE_URL).rstrip('/')
        self.api_key = api_key or settings.ARTEMIDA_API_KEY
        self._session = session
        self._owns_session = session is None

    async def __aenter__(self) -> Self:
        if self._session is None:
            self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _request(
        self, method: str, path: str, *, json: dict | None = None, idempotency_key: str | None = None
    ) -> dict:
        if not self.api_key:
            raise ArtemidaAPIError('ARTEMIDA_API_KEY is not configured')
        if self._session is None:
            raise ArtemidaAPIError('ArtemidaClient используется только как async context manager')
        headers = {'Authorization': f'Bearer {self.api_key}', 'Accept': 'application/json'}
        if idempotency_key:
            headers['Idempotency-Key'] = idempotency_key
        url = f'{self.base_url}{path}'
        try:
            async with self._session.request(method, url, json=json, headers=headers) as resp:
                try:
                    body = await resp.json()
                except (aiohttp.ContentTypeError, aiohttp.ClientPayloadError, ValueError) as exc:
                    # Тело не распарсилось: платный вызов мог уже отработать на вендоре —
                    # это НЕ успех, нельзя молча вернуть {}.
                    raise ArtemidaGatewayError(
                        f'Неразбираемый ответ вендора (HTTP {resp.status})', status=resp.status
                    ) from exc
                if resp.status >= 400 or body.get('ok') is False:
                    err = (body or {}).get('error') or {}
                    code = err.get('code')
                    message = err.get('message') or f'HTTP {resp.status}'
                    retry_after = None
                    try:
                        retry_after = (
                            float(resp.headers.get('Retry-After')) if resp.headers.get('Retry-After') else None
                        )
                    except (TypeError, ValueError):
                        retry_after = None
                    if code == 'insufficient_balance' or resp.status == 402:
                        # retry_after намеренно не передаётся: нехватку баланса не лечит ожидание.
                        raise ArtemidaInsufficientBalance(message, status=resp.status, code=code, data=body)
                    raise ArtemidaAPIError(message, status=resp.status, code=code, retry_after=retry_after, data=body)
                return body.get('data') or {}
        except (aiohttp.ClientError, TimeoutError) as exc:
            raise ArtemidaGatewayError(f'Сетевая ошибка при обращении к ARTΞMIDA: {exc}') from exc

    # --- keys ---
    async def create_key(
        self, *, days: int, devices: int, name: str, customer_ref: str, idempotency_key: str
    ) -> ArtemidaKey:
        data = await self._request(
            'POST',
            '/keys',
            json={
                'days': days,
                'devices': devices,
                'name': name,
                'customerRef': customer_ref,
            },
            idempotency_key=idempotency_key,
        )
        return ArtemidaKey.from_data(data.get('key') or {})

    async def get_key(self, key_id: str) -> ArtemidaKey:
        data = await self._request('GET', f'/keys/{key_id}')
        return ArtemidaKey.from_data(data.get('key') or {})

    async def renew_key(
        self, key_id: str, *, days: int, devices: int | None = None, idempotency_key: str
    ) -> ArtemidaKey:
        payload: dict[str, Any] = {'days': days}
        if devices is not None:
            payload['devices'] = devices
        data = await self._request('POST', f'/keys/{key_id}/renew', json=payload, idempotency_key=idempotency_key)
        return ArtemidaKey.from_data(data.get('key') or {})

    async def upgrade_key(self, key_id: str, *, devices: int, idempotency_key: str) -> ArtemidaKey:
        data = await self._request(
            'POST', f'/keys/{key_id}/upgrade', json={'devices': devices}, idempotency_key=idempotency_key
        )
        return ArtemidaKey.from_data(data.get('key') or {})

    async def revoke_key(self, key_id: str, *, idempotency_key: str) -> dict:
        return await self._request('DELETE', f'/keys/{key_id}', idempotency_key=idempotency_key)

    async def get_subscription_links(self, key_id: str) -> dict:
        return await self._request('GET', f'/keys/{key_id}/subscription-links')

    async def create_trial(self, *, idempotency_key: str) -> ArtemidaKey:
        data = await self._request('POST', '/trial', json={}, idempotency_key=idempotency_key)
        return ArtemidaKey.from_data(data.get('key') or {})

    async def get_balance(self) -> dict:
        return await self._request('GET', '/balance')

    async def get_pricing(self, *, devices: int, days: int) -> dict:
        return await self._request('GET', f'/pricing?devices={devices}&days={days}')
