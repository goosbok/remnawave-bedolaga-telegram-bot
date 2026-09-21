"""Сквозной e2e на моке вендора: покупка → провижининг → наша ссылка → ребренд-роут → swap → та же ссылка.

Ничего к настоящему API не ходит (Artemida замокана), денег не тратит. Все сессии
(провижининг, HTTP-роут, swap) работают с ОДНОЙ temp-file SQLite, поэтому роут
реально читает то, что записал провижининг. Тест и проверяет инварианты, и печатает
трассу цепочки (смотреть с ``-s``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.database.models import (
    Base,
    PromoGroup,
    Subscription,
    SubscriptionStatus,
    Tariff,
    tariff_promo_groups,
)
from app.services.provider_swap_service import move_subscription_to_provider
from app.services.providers import register_provider
from app.services.providers.artemida import ArtemidaProvider
from app.services.subscription_service import SubscriptionService
from app.webapi.routes import artemida_sub
from tests.fixtures.sqlite_memory import ensure_real_aiosqlite  # registers JSONB→JSON + real aiosqlite


_ARTEMIDA_LINKS = [
    'vless://11111111-1111-4111-8111-111111111111@de.artemida:443?type=tcp#Netherlands 1',
    'trojan://artpass@nl.artemida:443#NL 2',
]


class _FakeArtemidaClient:
    # Class-level so renew calls are visible across the fresh instance the provider
    # builds per `async with self._client_factory()`.
    renew_calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def create_key(self, **kw):
        return SimpleNamespace(
            id='art_key_1', devices=kw['devices'], subscription_url='https://vendor/x', expire_at=None
        )

    async def renew_key(self, key_id, *, days, devices=None, idempotency_key=None):
        # Records the PAID renew the money-critical seam issues via provider.update.
        type(self).renew_calls.append(
            {'key_id': key_id, 'days': days, 'devices': devices, 'idempotency_key': idempotency_key}
        )
        return SimpleNamespace(id=key_id, devices=devices, expire_at=None)

    async def get_subscription_links(self, key_id):
        return {'links': _ARTEMIDA_LINKS, 'count': len(_ARTEMIDA_LINKS), 'subscriptionUrl': 'https://vendor/x'}

    async def get_key(self, key_id):
        return SimpleNamespace(id=key_id, devices=3, subscription_url='https://vendor/x', expire_at=None)

    async def revoke_key(self, key_id, **kw):
        return {'ok': True}


class _FakeVendor2Provider:
    name = 'vendor2'

    async def provision(self, *, db, subscription, days):
        subscription.external_provider = self.name
        subscription.external_ref = 'v2_key_1'
        subscription.device_limit = getattr(subscription.tariff, 'device_limit', 3)
        base = 'https://sub.max/a'
        subscription.subscription_url = f'{base}/{subscription.public_token}'  # тот же public_token
        subscription.status = SubscriptionStatus.ACTIVE.value

    async def fetch_subscription(self, subscription, *, client_headers):
        # Same client link, different vendor's nodes, still our brand header.
        return b'[{"remarks":"vendor2 Paris"}]', 'application/json', {'profile-title': 'base64:TUFYIFZQTg=='}

    async def revoke(self, *, db, subscription):
        return None


@pytest.mark.asyncio
async def test_full_chain_provision_serve_swap(monkeypatch, tmp_path, capsys):
    ensure_real_aiosqlite(monkeypatch)
    db_file = tmp_path / 'e2e.db'
    engine = create_async_engine(f'sqlite+aiosqlite:///{db_file}')
    tables = [Tariff.__table__, PromoGroup.__table__, tariff_promo_groups, Subscription.__table__]
    async with engine.begin() as conn:
        await conn.run_sync(lambda c: Base.metadata.create_all(c, tables=tables))
    maker = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    # Роут читает БД через AsyncSessionLocal — направляем его на нашу temp-БД.
    monkeypatch.setattr(artemida_sub, 'AsyncSessionLocal', maker)
    # Настройки: база ребренд-ссылки (иначе provision честно падает на пустом URL).
    monkeypatch.setattr(
        'app.services.providers.artemida.settings.ARTEMIDA_REBRAND_BASE_URL', 'https://sub.max/a', raising=False
    )
    # renew_external() резолвит вендора через _external_provider_or_none, который
    # падает, если ARTEMIDA_ENABLED выключен — включаем для прогонки продления.
    monkeypatch.setattr('app.services.subscription_service.settings.ARTEMIDA_ENABLED', True, raising=False)
    # Мокаем клиента вендора — денег не тратим, к сети не ходим.
    monkeypatch.setattr('app.services.providers.artemida.ArtemidaClient', lambda *a, **k: _FakeArtemidaClient())
    # The serve step proxies the vendor's smart URL over HTTP — patch that seam so no
    # real network call happens; the route still rebrands the headers to ours.
    monkeypatch.setattr(
        'app.services.providers.artemida.ArtemidaProvider._fetch_vendor_document',
        AsyncMock(return_value=(b'[{"remarks":"artemida Germany"}]', 'application/json', {'profile-title': 'base64:vendor'})),
    )
    _FakeArtemidaClient.renew_calls = []
    # Регистрируем второго (фейкового) вендора для swap.
    from app.services import providers as providers_pkg

    saved = dict(providers_pkg._PROVIDERS)
    register_provider('vendor2', _FakeVendor2Provider)

    trace = []
    try:
        now = datetime.now(UTC)

        # 1) Покупка: тариф Максимум (artemida) + подписка на 30 дней.
        async with maker() as db:
            tariff = Tariff(
                name='Максимум',
                provider='artemida',
                device_limit=3,
                traffic_limit_gb=0,
                period_prices={'30': 49900},
                is_active=True,
            )
            db.add(tariff)
            await db.flush()
            sub = Subscription(
                user_id=1,
                tariff_id=tariff.id,
                start_date=now,
                end_date=now + timedelta(days=30),
                is_trial=False,
                status=SubscriptionStatus.PENDING.value,
            )
            db.add(sub)
            await db.commit()
            sub_id = sub.id
        trace.append(f'1. Покупка: подписка id={sub_id}, тариф Максимум (provider=artemida), 30 дней')

        # 2) Провижининг у вендора (мок): создаётся ключ, генерится наш public_token, строится ссылка.
        async with maker() as db:
            sub = await db.get(Subscription, sub_id)
            await db.refresh(sub, ['tariff'])
            await ArtemidaProvider().provision(db=db, subscription=sub, days=30)
            await db.commit()
            await db.refresh(sub)
            token = sub.public_token
            trace.append(
                f'2. Провижининг: external_provider={sub.external_provider}, '
                f'external_ref={sub.external_ref}, public_token={token}'
            )
            trace.append(f'   Ссылка клиенту: {sub.subscription_url}')
            assert sub.external_provider == 'artemida'
            assert sub.subscription_url == f'https://sub.max/a/{token}'
            assert '/art_key_1' not in sub.subscription_url  # НЕ id ключа вендора

        # 2b) Продление ещё на 30 дней: DB двигает end_date (как extend_subscription),
        #     а renew_external делает ПЛАТНЫЙ renew ключа у вендора — та самая дыра,
        #     из-за которой раньше клиент платил, а доступ у вендора не продлевался.
        async with maker() as db:
            sub = await db.get(Subscription, sub_id)
            await db.refresh(sub, ['tariff'])
            sub.end_date = sub.end_date + timedelta(days=30)  # как сделал бы extend_subscription
            await db.flush()
            renewed = await SubscriptionService().renew_external(db, sub, period_days=30)
            await db.refresh(sub)
            assert renewed is True  # для artemida вернул True (remnawave вернул бы False)
            assert len(_FakeArtemidaClient.renew_calls) == 1  # вендору ушёл ровно один платный renew
            assert _FakeArtemidaClient.renew_calls[0]['key_id'] == 'art_key_1'
            assert _FakeArtemidaClient.renew_calls[0]['days'] == 30
            assert sub.subscription_url == f'https://sub.max/a/{token}'  # ссылка клиенту НЕ меняется
            trace.append(
                f'2b. Продление +30д: renew_external→вендор renew '
                f'(days={_FakeArtemidaClient.renew_calls[0]["days"]}), ссылка та же {sub.subscription_url}'
            )

        # 3) Клиент открывает свою ссылку → ребренд-роут отдаёт переодетый конфиг.
        app = FastAPI()
        app.include_router(artemida_sub.router)
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
            r1 = await ac.get(f'/a/{token}')
        assert r1.status_code == 200
        # The vendor's real body is passed through (its country node names kept);
        # only the brand header is swapped to ours.
        assert r1.content == b'[{"remarks":"artemida Germany"}]'
        assert r1.headers['profile-title'] == 'base64:TUFYIFZQTg=='  # base64('MAX VPN'), not the vendor's
        trace.append(f'3. GET /a/{token} → 200, ноды artemida, бренд MAX')

        # 4) Artemida «заблокировали» → оператор жмёт swap на vendor2 (на остаток срока).
        async with maker() as db:
            sub = await db.get(Subscription, sub_id)
            await move_subscription_to_provider(db, sub, 'vendor2')
            await db.refresh(sub)
            trace.append(f'4. Swap → external_provider={sub.external_provider}, external_ref={sub.external_ref}')
            trace.append(f'   public_token={sub.public_token} (тот же), ссылка={sub.subscription_url} (та же)')
            assert sub.external_provider == 'vendor2'
            assert sub.public_token == token  # НЕ изменился
            assert sub.subscription_url == f'https://sub.max/a/{token}'  # НЕ изменилась

        # 5) Клиент по ТОЙ ЖЕ ссылке получает ноды нового вендора — бесшовно.
        async with AsyncClient(transport=ASGITransport(app=app), base_url='http://t') as ac:
            r2 = await ac.get(f'/a/{token}')
        assert r2.status_code == 200
        # Same client link, now serving the NEW vendor's nodes, still our brand.
        assert r2.content == b'[{"remarks":"vendor2 Paris"}]'
        assert r2.headers['profile-title'] == 'base64:TUFYIFZQTg=='
        trace.append(f'5. GET /a/{token} (та же ссылка) → 200, теперь ноды vendor2, бренд MAX')
    finally:
        providers_pkg._PROVIDERS.clear()
        providers_pkg._PROVIDERS.update(saved)
        await engine.dispose()

    with capsys.disabled():
        print('\n' + '\n'.join(trace))
