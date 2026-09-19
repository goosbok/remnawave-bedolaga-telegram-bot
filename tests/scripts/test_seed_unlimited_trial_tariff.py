"""Idempotent seed for the unlimited-trial Artemida tariff (Phase 4, two-trials feature).

`scripts/seed_unlimited_trial_tariff.py` must be safe to run against ANY DB after the
feature branch is deployed: it never creates a second unlimited-trial tariff, and a
dry run (the default) must not write anything at all.
"""

from __future__ import annotations

from app.database.crud.tariff import count_tariffs
from app.database.models import PromoGroup, Tariff, tariff_promo_groups
from app.services.unlimited_trial_service import resolve_unlimited_trial_tariff
from scripts.seed_unlimited_trial_tariff import seed_unlimited_trial_tariff
from tests.fixtures.sqlite_memory import memory_session


# Tariff.allowed_promo_groups is lazy='selectin' (see test_artemida_provision_e2e.py):
# persisting/refreshing a Tariff implicitly touches PromoGroup + the association table.
_TABLES = (Tariff.__table__, PromoGroup.__table__, tariff_promo_groups)


async def test_apply_on_empty_db_creates_the_tariff(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, created = await seed_unlimited_trial_tariff(db, apply=True)

        assert created is True
        assert tariff is not None
        assert tariff.provider == 'artemida'
        assert tariff.is_trial_available is True
        assert tariff.traffic_limit_gb == 0
        assert tariff.device_limit == 2
        assert tariff.is_active is True


async def test_running_it_again_is_a_noop(monkeypatch):
    """Idempotency: a second run must find the first tariff, not create a sibling."""
    async with memory_session(monkeypatch, _TABLES) as db:
        first, first_created = await seed_unlimited_trial_tariff(db, apply=True)
        assert first_created is True

        second, second_created = await seed_unlimited_trial_tariff(db, apply=True)

        assert second_created is False
        assert second is not None
        assert second.id == first.id

        total = await count_tariffs(db, include_inactive=True)
        assert total == 1


async def test_resolver_finds_the_seeded_tariff(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, _created = await seed_unlimited_trial_tariff(db, apply=True)

        resolved = await resolve_unlimited_trial_tariff(db)

        assert resolved is not None
        assert resolved.id == tariff.id


async def test_dry_run_does_not_persist_anything(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        tariff, created = await seed_unlimited_trial_tariff(db, apply=False)

        # "Would create" is still reported so the operator can tell dry run apart
        # from "already exists" — but nothing is built or written for it.
        assert created is True
        assert tariff is None

        total = await count_tariffs(db, include_inactive=True)
        assert total == 0


async def test_dry_run_reports_an_existing_tariff_without_duplicating_it(monkeypatch):
    async with memory_session(monkeypatch, _TABLES) as db:
        existing, _created = await seed_unlimited_trial_tariff(db, apply=True)

        tariff, created = await seed_unlimited_trial_tariff(db, apply=False)

        assert created is False
        assert tariff is not None
        assert tariff.id == existing.id

        total = await count_tariffs(db, include_inactive=True)
        assert total == 1
