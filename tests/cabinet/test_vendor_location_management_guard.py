"""Go-live guard: the cabinet must not expose OUR Remnawave squads as if they
were the locations of an *external-vendor* subscription.

External-vendor subs (``Subscription.is_external_vendor`` — Artemida "Максимум"/
"Команда"/unlimited-trial) are provisioned on a third-party vendor. Their real
server locations live in the vendor's rebranded ``/a/{token}`` config, NOT in our
local ``server_squads``. The country/location endpoints are provider-agnostic, so
without a guard they:

- list our own squads as this sub's "available countries" (``GET /countries``),
- and let the client PAY to "connect" a squad that does nothing for the vendor
  key (``POST /countries``).

The subscription DETAIL response (``status.py``) has the same leak: it resolves
``connected_squads`` (which vendor subs wrongly carry as our squad UUIDs) into
server-name chips.

The Remnawave path must stay byte-for-byte identical; only external-vendor subs
get the new behavior. Handlers are called directly with ``user=``/``db=``/
``subscription_id=`` kwargs against the in-memory session, mirroring
``tests/cabinet/test_free_tariff_purchase.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.database.models import (
    Base,
    ServerSquad,
    Subscription,
    SubscriptionStatus,
    User,
)
from tests.fixtures.sqlite_memory import memory_session


TABLES = list(Base.metadata.sorted_tables)


class _FakePanelSync:
    """Stand-in for SubscriptionService — the remnawave POST path syncs the panel."""

    async def update_remnawave_user(self, db, subscription, **kwargs):
        return SimpleNamespace(id=9001, used_traffic_bytes=0)

    async def create_remnawave_user(self, db, subscription, **kwargs):
        return SimpleNamespace(id=9001, used_traffic_bytes=0)


@pytest.fixture(autouse=True)
def _multi_tariff(monkeypatch):
    # resolve_subscription() takes the by-id path only in multi-tariff mode, which
    # lets these tests target a specific subscription deterministically.
    monkeypatch.setattr(settings, 'SALES_MODE', 'tariffs', raising=False)
    monkeypatch.setattr(settings, 'MULTI_TARIFF_ENABLED', True, raising=False)


@pytest.fixture
def panel(monkeypatch) -> _FakePanelSync:
    import app.cabinet.routes.subscription_modules.servers as servers_module

    monkeypatch.setattr(servers_module, 'SubscriptionService', _FakePanelSync)
    return _FakePanelSync()


def _user(*, balance_kopeks: int = 0) -> User:
    user = User(
        id=1,
        telegram_id=1001,
        first_name='U',
        language='ru',
        status='active',
        balance_kopeks=balance_kopeks,
    )
    # Neutralise lazy promo-group loads on the in-memory session (mirrors
    # tests/test_subscription_cart_integration.py): the /countries handlers call
    # PricingEngine.get_addon_discount_percent(user, ...), which walks promo groups.
    user.promo_group = None
    user.user_promo_groups = []
    return user


def _server_squad(*, uuid: str = 'sq-ru', name: str = 'Russia', price: int = 10000) -> ServerSquad:
    return ServerSquad(
        id=1,
        squad_uuid=uuid,
        display_name=name,
        country_code='RU',
        is_available=True,
        is_trial_eligible=False,
        price_kopeks=price,
        sort_order=0,
    )


def _subscription(
    *,
    external_provider: str | None = None,
    connected_squads: list[str] | None = None,
    remnawave_id: int | None = None,
    tariff_id: int | None = None,
) -> Subscription:
    now = datetime.now(UTC)
    return Subscription(
        id=10,
        user_id=1,
        status=SubscriptionStatus.ACTIVE.value,
        is_trial=False,
        start_date=now - timedelta(days=5),
        end_date=now + timedelta(days=25),
        traffic_limit_gb=100,
        traffic_used_gb=0.0,
        device_limit=2,
        connected_squads=connected_squads or [],
        external_provider=external_provider,
        remnawave_id=remnawave_id,
        remnawave_short_id='sub10',
        tariff_id=tariff_id,
    )


# ── GET /countries ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_countries_hides_local_squads_for_vendor_sub(monkeypatch):
    """Artemida sub → no local squads leaked, location management flagged off."""
    from app.cabinet.routes.subscription_modules.servers import get_available_countries

    async with memory_session(monkeypatch, TABLES) as db:
        user = _user()
        db.add_all(
            [
                user,
                _server_squad(),
                _subscription(external_provider='artemida', connected_squads=['sq-ru']),
            ]
        )
        await db.commit()

        response = await get_available_countries(user=user, db=db, subscription_id=10)

    assert response['countries'] == []
    assert response['location_management_available'] is False
    assert response['has_subscription'] is True
    assert response['connected_count'] == 0


@pytest.mark.asyncio
async def test_get_countries_lists_squads_for_remnawave_sub(monkeypatch):
    """Remnawave sub → squads still enumerated, location management flagged on."""
    from app.cabinet.routes.subscription_modules.servers import get_available_countries

    async with memory_session(monkeypatch, TABLES) as db:
        user = _user()
        db.add_all(
            [
                user,
                _server_squad(uuid='sq-ru', name='Russia'),
                _subscription(external_provider=None, connected_squads=[]),
            ]
        )
        await db.commit()

        response = await get_available_countries(user=user, db=db, subscription_id=10)

    assert response['location_management_available'] is True
    assert [c['uuid'] for c in response['countries']] == ['sq-ru']


# ── POST /countries ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_countries_rejects_vendor_sub_without_charge(monkeypatch, panel):
    """Artemida sub → rejected before any balance/squad mutation."""
    from app.cabinet.routes.subscription_modules.servers import update_countries

    async with memory_session(monkeypatch, TABLES) as db:
        user = _user(balance_kopeks=1_000_000)
        db.add_all(
            [
                user,
                _server_squad(uuid='sq-ru', price=10000),
                _subscription(external_provider='artemida', connected_squads=[]),
            ]
        )
        await db.commit()

        with pytest.raises(HTTPException) as exc_info:
            await update_countries(
                request={'countries': ['sq-ru']},
                user=user,
                db=db,
                subscription_id=10,
            )

        assert 400 <= exc_info.value.status_code < 500

        # No charge and no squad change happened.
        refreshed_user = await db.get(User, 1)
        assert refreshed_user.balance_kopeks == 1_000_000
        subscription = await db.get(Subscription, 10)
        assert (subscription.connected_squads or []) == []


@pytest.mark.asyncio
async def test_post_countries_still_charges_remnawave_sub(monkeypatch, panel):
    """Remnawave sub → paid squad is added and charged, exactly as before."""
    from app.cabinet.routes.subscription_modules.servers import update_countries

    async with memory_session(monkeypatch, TABLES) as db:
        user = _user(balance_kopeks=1_000_000)
        db.add_all(
            [
                user,
                _server_squad(uuid='sq-ru', price=10000),
                _subscription(external_provider=None, connected_squads=[], remnawave_id=9001),
            ]
        )
        await db.commit()

        response = await update_countries(
            request={'countries': ['sq-ru']},
            user=user,
            db=db,
            subscription_id=10,
        )

        assert 'sq-ru' in (response.get('connected_squads') or [])
        refreshed_user = await db.get(User, 1)
        assert refreshed_user.balance_kopeks < 1_000_000  # charge went through


# ── GET /subscription/info (status) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_hides_server_chips_for_vendor_sub(monkeypatch):
    """Artemida sub with connected_squads set → no server chips, no squads leaked."""
    from app.cabinet.routes.subscription_modules.status import get_subscription

    async with memory_session(monkeypatch, TABLES) as db:
        user = _user()
        db.add_all(
            [
                user,
                _server_squad(uuid='sq-ru', name='Russia'),
                _subscription(external_provider='artemida', connected_squads=['sq-ru']),
            ]
        )
        await db.commit()

        response = await get_subscription(user=user, db=db, subscription_id=10)

    assert response.has_subscription is True
    assert response.subscription is not None
    assert response.subscription.servers == []
    assert response.subscription.connected_squads == []


@pytest.mark.asyncio
async def test_status_resolves_server_chips_for_remnawave_sub(monkeypatch):
    """Remnawave sub with connected_squads → server names still resolved, unchanged."""
    from app.cabinet.routes.subscription_modules.status import get_subscription

    async with memory_session(monkeypatch, TABLES) as db:
        user = _user()
        db.add_all(
            [
                user,
                _server_squad(uuid='sq-ru', name='Russia'),
                _subscription(external_provider=None, connected_squads=['sq-ru']),
            ]
        )
        await db.commit()

        response = await get_subscription(user=user, db=db, subscription_id=10)

    assert response.subscription is not None
    assert [s.uuid for s in response.subscription.servers] == ['sq-ru']
    assert [s.name for s in response.subscription.servers] == ['Russia']
    assert response.subscription.connected_squads == ['sq-ru']
