#!/usr/bin/env python
"""Idempotent seed for the unlimited-trial (Artemida) tariff.

``activate_unlimited_trial()`` (``app/services/unlimited_trial_service.py``) needs
a ``Tariff`` row with ``provider='artemida'`` and ``is_trial_available=True`` to
resolve against — this script creates exactly that row, once.

WARNING — run this ONLY after the feature branch is deployed AND Alembic
migrations ``0120`` (``artemida_provider``) and ``0121``
(``artemida_external_ref_index``) have been applied. Those migrations add the
``Tariff.provider``/``provider_opts`` columns this script writes to; on a DB that
predates them, INSERT fails outright.

It is independent of ``ARTEMIDA_ENABLED``/``ARTEMIDA_TRIAL_ENABLED``: the tariff
row can exist inertly on any environment, including one where the Artemida vendor
integration is still turned off in config. ``resolve_unlimited_trial_tariff()``
only starts returning it once those flags are also on, so seeding early (right
after migrating) is safe and does not itself turn anything on for real users.

Usage:
    python -m scripts.seed_unlimited_trial_tariff            # dry run, writes nothing
    python -m scripts.seed_unlimited_trial_tariff --apply    # persist

Dry run is the default on purpose, same as ``scripts/backfill_remnawave_ids.py``:
read the report, and only then re-run with ``--apply``. Idempotent either way — if
an artemida trial tariff already exists (active or not), the script reports it and
does nothing, rather than creating a second one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.crud.tariff import create_tariff
from app.database.database import AsyncSessionLocal
from app.database.models import Tariff


logger = structlog.get_logger(__name__)

# Cosmetic only: the trial is never actually sold at this tariff's own price/
# duration.  activate_unlimited_trial() hardcodes duration_days=1 and
# traffic_limit_gb=0 at activation time regardless of what's stored here.
TARIFF_NAME = 'Безлимит · 1 день'


async def _find_existing_unlimited_trial_tariff(db: AsyncSession) -> Tariff | None:
    """Любой artemida-тариф с is_trial_available, независимо от is_active.

    Не резолвер: резолвер ещё и учитывает ``ARTEMIDA_TRIAL_TARIFF_ID`` из
    настроек, а сиду нужен факт «такая строка уже где-то есть», а не «сейчас
    активно используется» — иначе при выставленном (например, вручную) другом
    ``ARTEMIDA_TRIAL_TARIFF_ID`` сид создаст дубль поверх уже существующего.
    """
    result = await db.execute(
        select(Tariff)
        .where(Tariff.provider == 'artemida', Tariff.is_trial_available.is_(True))
        .order_by(Tariff.id)
        .limit(1)
    )
    return result.scalar_one_or_none()


async def seed_unlimited_trial_tariff(
    db: AsyncSession,
    *,
    apply: bool = False,
) -> tuple[Tariff | None, bool]:
    """Идемпотентно создаёт безлимит-триальный (Artemida) тариф.

    Returns:
        ``(tariff, created)``:

        * такой тариф уже существует -> ``(existing_tariff, False)``, БД не тронута.
        * тариф не существует и ``apply=False`` (dry run) -> ``(None, True)`` —
          «создал бы», но ничего не построено и не записано.
        * тариф не существует и ``apply=True`` -> ``(new_tariff, True)``, закоммичено.
    """
    existing = await _find_existing_unlimited_trial_tariff(db)
    if existing is not None:
        return existing, False

    if not apply:
        return None, True

    tariff = await create_tariff(
        db=db,
        name=TARIFF_NAME,
        # Как и обычный триальный тариф (см. докстринг get_trial_tariff):
        # is_active=False специально, чтобы тариф не отображался в списке
        # покупки (иначе это бесплатный тариф на 30 дней в общем списке — обход
        # оплаты), но is_trial_available=True всё равно резолвится для триала
        # (resolve_unlimited_trial_tariff больше не требует is_active).
        is_active=False,
        is_trial_available=True,
        traffic_limit_gb=0,
        device_limit=2,
        # Цена/период — формальность ради валидатора create_tariff: триал
        # бесплатный и всегда на 1 день, что подставляет activate_unlimited_trial().
        period_prices={30: 0},
        show_in_gift=False,
        provider='artemida',
    )
    return tariff, True


async def _run(apply: bool) -> int:
    async with AsyncSessionLocal() as db:
        tariff, created = await seed_unlimited_trial_tariff(db, apply=apply)

    print()
    if tariff is None:
        # dry run, ничего не найдено — созданный тариф не построен вовсе.
        print('  DRY RUN: тарифа нет, был бы создан безлимит-триальный (Artemida) тариф')
        print(f'           name={TARIFF_NAME!r}, provider=artemida, is_trial_available=True')
        logger.info('seed_unlimited_trial_tariff: dry run, would create', apply=apply)
    elif created:
        print(f'  СОЗДАН безлимит-триальный (Artemida) тариф: id={tariff.id} name={tariff.name!r}')
        logger.info('seed_unlimited_trial_tariff: created', tariff_id=tariff.id, apply=apply)
    else:
        print(f'  УЖЕ СУЩЕСТВУЕТ безлимит-триальный (Artemida) тариф: id={tariff.id} name={tariff.name!r}')
        logger.info('seed_unlimited_trial_tariff: already exists', tariff_id=tariff.id, apply=apply)
    print()

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Seed the unlimited-trial (Artemida) tariff (idempotent; dry run by default)'
    )
    parser.add_argument('--apply', action='store_true', help='persist changes (default is a dry run)')
    args = parser.parse_args()
    return asyncio.run(_run(args.apply))


if __name__ == '__main__':
    sys.exit(main())
