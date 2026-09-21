#!/usr/bin/env python
"""Idempotent seed for the paid Artemida vendor tariffs (Максимум, Команда).

These are the two client-facing, FOR-SALE tariffs provisioned on the external
Artemida vendor (unlike the unlimited-trial tariff, which is a hidden free row —
see ``scripts/seed_unlimited_trial_tariff.py``):

* **Максимум** — 3 devices, unlimited traffic, 499 ₽ / 30 days.
* **Команда**  — 15 devices, unlimited traffic, 1499 ₽ / 30 days.

Both are ``provider='artemida'``, ``is_active=True`` (they DO appear in the
purchase list), ``traffic_limit_gb=0`` (unlimited). Device purchase is pinned off
(``max_device_limit=device_limit``, matching the service-wide policy) and traffic
top-up is disabled (``allow_traffic_topup=False``) — the vendor has no primitive
for a mid-cycle traffic top-up and these tariffs are unlimited anyway.

Only the 30-day price is seeded on purpose. The vendor has NO annual discount, so a
long-period price must be validated against the sum of the ≤90-day chunk quotes from
``GET /v1/pricing`` before it is offered (a flat annual price below 12× the monthly
vendor cost sells at a loss). Add 90/180/360-day prices via the admin UI only after
that check.

The third tariff in the line-up, **Активный** (300 ₽, own Remnawave nodes,
full-tunnel), is intentionally NOT seeded here: it is a ``provider='remnawave'``
tariff whose ``allowed_squads`` must point at the production full-tunnel squad
(prod-specific UUID) with the whitelist removed at the panel/Happ routing level.
Create it in the admin UI where that squad can be selected.

WARNING — run this ONLY after the feature branch is deployed AND Alembic migration
``0120`` (``artemida_provider``) has been applied; it adds the ``Tariff.provider``
column this script writes to. It is independent of ``ARTEMIDA_ENABLED``: the rows
can exist inertly while the vendor integration is still turned off in config.

Usage:
    python -m scripts.seed_vendor_tariffs            # dry run, writes nothing
    python -m scripts.seed_vendor_tariffs --apply    # persist

Dry run is the default on purpose: read the report, and only then re-run with
``--apply``. Idempotent either way — a tariff that already exists (by name +
provider, active or not) is reported and left untouched, never duplicated.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.tariff import create_tariff
from app.database.database import AsyncSessionLocal
from app.database.models import Tariff


logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class _VendorTariffSpec:
    name: str
    device_limit: int
    price_kopeks_30d: int
    description: str


# 30-day price only (see module docstring on why long periods are deferred).
_SPECS: tuple[_VendorTariffSpec, ...] = (
    _VendorTariffSpec(
        name='Максимум',
        device_limit=3,
        price_kopeks_30d=49900,
        description='Безлимитный трафик, 3 устройства',
    ),
    _VendorTariffSpec(
        name='Команда',
        device_limit=15,
        price_kopeks_30d=149900,
        description='Безлимитный трафик, 15 устройств',
    ),
)


async def _find_existing(db: AsyncSession, name: str) -> Tariff | None:
    """An artemida tariff with this exact name, regardless of is_active.

    Keyed on (name, provider) rather than the resolver so a second run finds the
    row it created even if the operator later toggled it inactive.
    """
    result = await db.execute(
        select(Tariff).where(Tariff.provider == 'artemida', Tariff.name == name).order_by(Tariff.id).limit(1)
    )
    return result.scalar_one_or_none()


async def _seed_one(db: AsyncSession, spec: _VendorTariffSpec, *, apply: bool) -> tuple[Tariff | None, bool]:
    existing = await _find_existing(db, spec.name)
    if existing is not None:
        return existing, False

    if not apply:
        return None, True

    tariff = await create_tariff(
        db=db,
        name=spec.name,
        description=spec.description,
        is_active=True,
        traffic_limit_gb=0,
        device_limit=spec.device_limit,
        # Докупка устройств выключена так же, как в остальном сервисе: верхний
        # предел равен базовому, поэтому «+устройство» недоступно.
        max_device_limit=spec.device_limit,
        # Топ-ап трафика недоступен: у вендора нет примитива на докупку трафика
        # среди срока, а тариф и так безлимитный.
        allow_traffic_topup=False,
        traffic_topup_enabled=False,
        period_prices={30: spec.price_kopeks_30d},
        provider='artemida',
    )
    return tariff, True


async def seed_vendor_tariffs(
    db: AsyncSession,
    *,
    apply: bool = False,
) -> list[tuple[_VendorTariffSpec, Tariff | None, bool]]:
    """Idempotently create the paid Artemida vendor tariffs.

    Returns one ``(spec, tariff, created)`` per spec, mirroring
    ``seed_unlimited_trial_tariff``:

    * already exists -> ``(spec, existing_tariff, False)``, DB untouched.
    * missing and ``apply=False`` (dry run) -> ``(spec, None, True)`` — "would
      create", nothing built or written.
    * missing and ``apply=True`` -> ``(spec, new_tariff, True)``, committed.
    """
    results: list[tuple[_VendorTariffSpec, Tariff | None, bool]] = []
    for spec in _SPECS:
        tariff, created = await _seed_one(db, spec, apply=apply)
        results.append((spec, tariff, created))
    return results


async def _run(apply: bool) -> int:
    async with AsyncSessionLocal() as db:
        results = await seed_vendor_tariffs(db, apply=apply)

    print()
    for spec, tariff, created in results:
        if tariff is None:
            print(
                f'  DRY RUN: тарифа нет, был бы создан {spec.name!r} '
                f'(provider=artemida, device_limit={spec.device_limit}, '
                f'price_30d={spec.price_kopeks_30d / 100:.0f} ₽)'
            )
            logger.info('seed_vendor_tariffs: dry run, would create', name=spec.name, apply=apply)
        elif created:
            print(f'  СОЗДАН тариф {spec.name!r}: id={tariff.id}, device_limit={spec.device_limit}')
            logger.info('seed_vendor_tariffs: created', name=spec.name, tariff_id=tariff.id, apply=apply)
        else:
            print(f'  УЖЕ СУЩЕСТВУЕТ тариф {spec.name!r}: id={tariff.id}')
            logger.info('seed_vendor_tariffs: already exists', name=spec.name, tariff_id=tariff.id, apply=apply)
    print()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Seed the paid Artemida vendor tariffs Максимум/Команда (idempotent; dry run by default)'
    )
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    args = parser.parse_args()
    return asyncio.run(_run(args.apply))


if __name__ == '__main__':
    sys.exit(main())
