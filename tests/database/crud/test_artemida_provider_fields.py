"""Provider fields backing the Artemida vendor-integration feature.

`Tariff.provider` keys the whole feature: 'remnawave' (own panel, default) vs
'artemida' (external vendor). `Tariff.provider_opts` carries vendor-specific
options (e.g. Artemida location pinning). `Subscription.external_provider`/
`external_ref` store the vendor's own key once a subscription is actually
provisioned by it.

SQLAlchemy applies Column `default=` values at INSERT/flush time, not on bare
in-memory construction, so both defaults only resolve after a flush against a
real session.
"""

from datetime import UTC, datetime, timedelta

from app.database.crud.tariff import create_tariff
from app.database.models import PromoGroup, Subscription, Tariff, tariff_promo_groups
from tests.fixtures.sqlite_memory import memory_session


async def test_tariff_provider_defaults_to_remnawave(monkeypatch):
    async with memory_session(monkeypatch, (Tariff.__table__,)) as db:
        tariff = Tariff(name='X', period_prices={'30': 49900})
        db.add(tariff)
        await db.commit()

        assert tariff.provider == 'remnawave'
        assert tariff.provider_opts == {}


async def test_create_tariff_defaults_to_remnawave(monkeypatch):
    """`create_tariff` predates `provider`/`provider_opts`: the new keyword-only
    params must default to today's only tariff shape without changing it."""
    tables = (Tariff.__table__, PromoGroup.__table__, tariff_promo_groups)

    async with memory_session(monkeypatch, tables) as db:
        tariff = await create_tariff(db=db, name='Базовый', period_prices={30: 60000})

        assert tariff.provider == 'remnawave'
        assert tariff.provider_opts == {}


async def test_create_tariff_persists_provider_artemida(monkeypatch):
    """The Artemida vendor path provisions a tariff whose `provider='artemida'`;
    `create_tariff` must be able to build one directly, with its own opts."""
    tables = (Tariff.__table__, PromoGroup.__table__, tariff_promo_groups)

    async with memory_session(monkeypatch, tables) as db:
        tariff = await create_tariff(
            db=db,
            name='Безлимит',
            period_prices={30: 0},
            provider='artemida',
            provider_opts={'location': 'eu'},
        )

        assert tariff.provider == 'artemida'
        assert tariff.provider_opts == {'location': 'eu'}


async def test_subscription_external_ref_defaults_none(monkeypatch):
    async with memory_session(monkeypatch, (Subscription.__table__,)) as db:
        subscription = Subscription(user_id=1, end_date=datetime.now(UTC) + timedelta(days=30))
        db.add(subscription)
        await db.commit()
        await db.refresh(subscription)

        assert subscription.external_provider is None
        assert subscription.external_ref is None
