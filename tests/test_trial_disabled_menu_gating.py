"""Regression tests: the bot must hide/refuse the trial when it is disabled
(TRIAL_DURATION_DAYS <= 0 or TRIAL_DISABLED_FOR == 'all'), matching the
mini-app/cabinet behaviour.

Covers all three render/grant surfaces:
  1. get_main_menu_keyboard (default sync keyboard)
  2. MenuLayoutService._evaluate_conditions (custom-menu constructor path)
  3. show_trial_offer / activate_trial handlers

The flip side is covered too, right below each surface's "hides" tests: none
of these three surfaces may hide/dead-end the trial entry when the UNLIMITED
(Artemida) trial is available, even though the LIMITED trial itself is off or
already used — the two trials are independent offers, not one gate.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.types import CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User


def _menu_has_trial(markup) -> bool:
    return any(getattr(btn, 'callback_data', None) == 'menu_trial' for row in markup.inline_keyboard for btn in row)


# --- Surface 1: default sync keyboard -------------------------------------


def test_keyboard_hides_trial_when_duration_zero():
    from app.keyboards.inline import get_main_menu_keyboard

    orig = settings.TRIAL_DURATION_DAYS
    try:
        settings.TRIAL_DURATION_DAYS = 0
        kb = get_main_menu_keyboard(has_had_paid_subscription=False, has_active_subscription=False)
        assert not _menu_has_trial(kb)

        settings.TRIAL_DURATION_DAYS = 3
        kb = get_main_menu_keyboard(has_had_paid_subscription=False, has_active_subscription=False)
        assert _menu_has_trial(kb)
    finally:
        settings.TRIAL_DURATION_DAYS = orig


def test_keyboard_hides_trial_when_disabled_for_all():
    from app.keyboards.inline import get_main_menu_keyboard

    orig_days, orig_disabled = settings.TRIAL_DURATION_DAYS, settings.TRIAL_DISABLED_FOR
    try:
        settings.TRIAL_DURATION_DAYS = 3
        settings.TRIAL_DISABLED_FOR = 'all'
        kb = get_main_menu_keyboard(has_had_paid_subscription=False, has_active_subscription=False)
        assert not _menu_has_trial(kb)
    finally:
        settings.TRIAL_DURATION_DAYS, settings.TRIAL_DISABLED_FOR = orig_days, orig_disabled


def test_keyboard_shows_trial_entry_when_only_unlimited_trial_available(monkeypatch):
    """The 'Триал' menu entry shows when only the unlimited trial is on.

    Feature-level gate: limited trial fully off (duration 0), but the
    Artemida unlimited trial is enabled. No per-user check here — the trial
    screen itself does that.
    """
    from app.keyboards.inline import get_main_menu_keyboard

    monkeypatch.setattr(settings, 'ARTEMIDA_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ARTEMIDA_TRIAL_ENABLED', True, raising=False)

    orig_days = settings.TRIAL_DURATION_DAYS
    try:
        settings.TRIAL_DURATION_DAYS = 0
        kb = get_main_menu_keyboard(has_had_paid_subscription=False, has_active_subscription=False)
        assert _menu_has_trial(kb)
    finally:
        settings.TRIAL_DURATION_DAYS = orig_days


def test_keyboard_still_hides_trial_when_unlimited_flags_off(monkeypatch):
    """The widened condition changes nothing when unlimited is off.

    Sanity check: with both Artemida flags explicitly off, a disabled limited
    trial hides the entry exactly like before this feature existed.
    """
    from app.keyboards.inline import get_main_menu_keyboard

    monkeypatch.setattr(settings, 'ARTEMIDA_ENABLED', False, raising=False)
    monkeypatch.setattr(settings, 'ARTEMIDA_TRIAL_ENABLED', False, raising=False)

    orig_days = settings.TRIAL_DURATION_DAYS
    try:
        settings.TRIAL_DURATION_DAYS = 0
        kb = get_main_menu_keyboard(has_had_paid_subscription=False, has_active_subscription=False)
        assert not _menu_has_trial(kb)
    finally:
        settings.TRIAL_DURATION_DAYS = orig_days


# --- Surface 2: custom-menu constructor path ------------------------------


def test_menu_layout_hides_trial_when_disabled():
    from app.services.menu_layout import MenuContext
    from app.services.menu_layout.service import MenuLayoutService

    ctx = MenuContext(has_had_paid_subscription=False, has_active_subscription=False)
    cond = {'show_trial': True}

    orig_days, orig_disabled = settings.TRIAL_DURATION_DAYS, settings.TRIAL_DISABLED_FOR
    try:
        settings.TRIAL_DURATION_DAYS = 3
        settings.TRIAL_DISABLED_FOR = 'none'
        assert MenuLayoutService._evaluate_conditions(cond, ctx) is True

        settings.TRIAL_DURATION_DAYS = 0
        assert MenuLayoutService._evaluate_conditions(cond, ctx) is False

        settings.TRIAL_DURATION_DAYS = 3
        settings.TRIAL_DISABLED_FOR = 'all'
        assert MenuLayoutService._evaluate_conditions(cond, ctx) is False
    finally:
        settings.TRIAL_DURATION_DAYS, settings.TRIAL_DISABLED_FOR = orig_days, orig_disabled


def test_menu_layout_shows_trial_entry_when_only_unlimited_trial_available(monkeypatch):
    """The custom-menu constructor gets the same feature-level widening.

    Same rule as the default keyboard (Surface 1), applied to
    MenuLayoutService's own condition evaluator.
    """
    from app.services.menu_layout import MenuContext
    from app.services.menu_layout.service import MenuLayoutService

    monkeypatch.setattr(settings, 'ARTEMIDA_ENABLED', True, raising=False)
    monkeypatch.setattr(settings, 'ARTEMIDA_TRIAL_ENABLED', True, raising=False)

    ctx = MenuContext(has_had_paid_subscription=False, has_active_subscription=False)
    cond = {'show_trial': True}

    orig_days, orig_disabled = settings.TRIAL_DURATION_DAYS, settings.TRIAL_DISABLED_FOR
    try:
        settings.TRIAL_DURATION_DAYS = 0
        assert MenuLayoutService._evaluate_conditions(cond, ctx) is True

        settings.TRIAL_DURATION_DAYS = 3
        settings.TRIAL_DISABLED_FOR = 'all'
        assert MenuLayoutService._evaluate_conditions(cond, ctx) is True
    finally:
        settings.TRIAL_DURATION_DAYS, settings.TRIAL_DISABLED_FOR = orig_days, orig_disabled


# --- Surface 3: handlers --------------------------------------------------


def _make_cb_user_db():
    cb = AsyncMock(spec=CallbackQuery)
    cb.message = AsyncMock(spec=Message)
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    user = MagicMock(spec=User)
    user.language = 'ru'
    user.auth_type = 'telegram'
    user.restriction_subscription = False
    user.is_trial_already_used = MagicMock(return_value=False)
    db = AsyncMock(spec=AsyncSession)
    return cb, user, db


@pytest.mark.asyncio
async def test_show_trial_offer_blocks_when_duration_zero():
    from app.handlers.subscription import purchase

    cb, user, db = _make_cb_user_db()
    orig = settings.TRIAL_DURATION_DAYS
    try:
        settings.TRIAL_DURATION_DAYS = 0
        await purchase.show_trial_offer(cb, user, db)
        cb.message.edit_text.assert_awaited_once()
        # Returned before the eligibility/used check — proves the guard fired.
        user.is_trial_already_used.assert_not_called()
    finally:
        settings.TRIAL_DURATION_DAYS = orig


@pytest.mark.asyncio
async def test_activate_trial_blocks_when_duration_zero():
    from app.handlers.subscription import purchase

    cb, user, db = _make_cb_user_db()
    orig = settings.TRIAL_DURATION_DAYS
    try:
        settings.TRIAL_DURATION_DAYS = 0
        await purchase.activate_trial(cb, user, db)
        cb.message.edit_text.assert_awaited_once()
        user.is_trial_already_used.assert_not_called()
    finally:
        settings.TRIAL_DURATION_DAYS = orig


def _keyboard_callbacks(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]


@pytest.mark.asyncio
async def test_show_trial_offer_reaches_unlimited_screen_when_duration_zero(monkeypatch):
    """Duration-zero must no longer dead-end when unlimited is available.

    It renders the unlimited-only offer instead of 'Пробный период недоступен'.
    """
    from app.handlers.subscription import purchase

    cb, user, db = _make_cb_user_db()
    monkeypatch.setattr('app.services.unlimited_trial_service.unlimited_trial_available', lambda u: True)

    orig = settings.TRIAL_DURATION_DAYS
    try:
        settings.TRIAL_DURATION_DAYS = 0
        await purchase.show_trial_offer(cb, user, db)
    finally:
        settings.TRIAL_DURATION_DAYS = orig

    cb.message.edit_text.assert_awaited_once()
    body, kwargs = cb.message.edit_text.call_args[0][0], cb.message.edit_text.call_args[1]
    assert body == '🚀 Доступен безлимитный пробный период на 1 день'

    callbacks = _keyboard_callbacks(kwargs['reply_markup'])
    assert 'activate_unlimited_trial' in callbacks
    assert 'trial_activate' not in callbacks
    assert 'back_to_menu' in callbacks


@pytest.mark.asyncio
async def test_show_trial_offer_reaches_unlimited_screen_when_limited_already_used(monkeypatch):
    """A limited-trial-used user must still reach the unlimited offer.

    When they haven't used THAT one, the old 'TRIAL_ALREADY_USED' dead-end
    must not swallow this case.
    """
    from app.handlers.subscription import purchase

    cb, user, db = _make_cb_user_db()
    user.is_trial_already_used = MagicMock(return_value=True)
    monkeypatch.setattr('app.services.unlimited_trial_service.unlimited_trial_available', lambda u: True)

    await purchase.show_trial_offer(cb, user, db)

    cb.message.edit_text.assert_awaited_once()
    body, kwargs = cb.message.edit_text.call_args[0][0], cb.message.edit_text.call_args[1]
    assert body == '🚀 Доступен безлимитный пробный период на 1 день'

    callbacks = _keyboard_callbacks(kwargs['reply_markup'])
    assert 'activate_unlimited_trial' in callbacks
    assert 'trial_activate' not in callbacks


@pytest.mark.asyncio
async def test_show_trial_offer_still_dead_ends_when_neither_trial_available(monkeypatch):
    """The original dead-end is unchanged when neither trial is available.

    Regression: limited trial used up AND unlimited trial unavailable.
    """
    from app.handlers.subscription import purchase

    cb, user, db = _make_cb_user_db()
    user.is_trial_already_used = MagicMock(return_value=True)
    monkeypatch.setattr('app.services.unlimited_trial_service.unlimited_trial_available', lambda u: False)

    await purchase.show_trial_offer(cb, user, db)

    cb.message.edit_text.assert_awaited_once()
    body = cb.message.edit_text.call_args[0][0]
    assert body == '❌ Тестовая подписка уже была использована'
