"""Idempotent seed for the paid Artemida vendor tariffs (Максимум, Команда).

`scripts/seed_vendor_tariffs.py` must be safe to run against ANY DB after the
feature branch is deployed: it never creates a duplicate of a tariff that already
exists (by name + provider), and a dry run (the default) must not write anything.
"""

from __future__ import annotations

from app.database.crud.tariff import count_tariffs
from app.database.models import PromoGroup, Tariff, tariff_promo_groups
from scripts.seed_vendor_tariffs import seed_vendor_tariffs
from tests.fixtures.sqlite_memory import memory_session


# Tariff.allowed_promo_groups is lazy='selectin' (see test_artemida_provision_e2e.py):
# persisting/refreshing a Tariff implicitly touches PromoGroup + the association table.
_TABLES = (Tariff.__table__, PromoGroup.__table__, tariff_promo_groups)


async def test_apply_on_empty_db_creates_both_tariffs(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        results = await seed_vendor_tariffs(db, apply=True)

        by_name = {spec.name: (tariff, created) for spec, tariff, created in results}
        assert set(by_name) == {'Максимум', 'Команда'}

        maximum, maximum_created = by_name['Максимум']
        assert maximum_created is True
        assert maximum is not None
        assert maximum.provider == 'artemida'
        assert maximum.is_active is True  # FOR SALE — visible in the purchase list
        assert maximum.traffic_limit_gb == 0  # unlimited
        assert maximum.device_limit == 3
        # Device purchase pinned off: the upper bound equals the base.
        assert maximum.max_device_limit == 3
        # Only the 30-day price is seeded (long periods need vendor-cost validation).
        assert maximum.period_prices == {'30': 49900}

        team, team_created = by_name['Команда']
        assert team_created is True
        assert team is not None
        assert team.device_limit == 15
        assert team.max_device_limit == 15
        assert team.period_prices == {'30': 149900}

        total = await count_tariffs(db, include_inactive=True)
        assert total == 2


async def test_running_it_again_is_a_noop(monkeypatch):
    """Idempotency: a second run must find the first rows, not create siblings."""
    async with memory_session(monkeypatch, _TABLES) as db:
        first = await seed_vendor_tariffs(db, apply=True)
        first_ids = {spec.name: tariff.id for spec, tariff, _created in first}

        second = await seed_vendor_tariffs(db, apply=True)

        for spec, tariff, created in second:
            assert created is False
            assert tariff is not None
            assert tariff.id == first_ids[spec.name]

        total = await count_tariffs(db, include_inactive=True)
        assert total == 2


async def test_dry_run_does_not_persist_anything(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        results = await seed_vendor_tariffs(db, apply=False)

        # "Would create" is still reported (created=True, tariff=None) so the
        # operator can tell dry run apart from "already exists".
        for _spec, tariff, created in results:
            assert created is True
            assert tariff is None

        total = await count_tariffs(db, include_inactive=True)
        assert total == 0


async def test_dry_run_reports_existing_without_duplicating(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        applied = await seed_vendor_tariffs(db, apply=True)
        existing_ids = {spec.name: tariff.id for spec, tariff, _created in applied}

        results = await seed_vendor_tariffs(db, apply=False)

        for spec, tariff, created in results:
            assert created is False
            assert tariff is not None
            assert tariff.id == existing_ids[spec.name]

        total = await count_tariffs(db, include_inactive=True)
        assert total == 2


async def test_partial_seed_only_creates_the_missing_one(monkeypatch):
    """If one tariff already exists, --apply creates only the other, no duplicate."""
    from app.database.crud.tariff import create_tariff

    async with memory_session(monkeypatch, _TABLES) as db:
        # Simulate a half-seeded DB: Максимум already present, Команда missing.
        await create_tariff(
            db=db,
            name='Максимум',
            is_active=True,
            traffic_limit_gb=0,
            device_limit=3,
            period_prices={30: 49900},
            provider='artemida',
        )
        assert await count_tariffs(db, include_inactive=True) == 1

        results = await seed_vendor_tariffs(db, apply=True)
        by_name = {spec.name: created for spec, _tariff, created in results}
        assert by_name['Максимум'] is False  # already there — not duplicated
        assert by_name['Команда'] is True  # the missing one is created

        assert await count_tariffs(db, include_inactive=True) == 2
