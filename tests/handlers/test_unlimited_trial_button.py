"""Tests for the unlimited-trial (Artemida) button and handler in the bot.

Covers:
  1. ``get_trial_keyboard`` (app/keyboards/inline.py): shows the
     unlimited-trial button only when ``unlimited_trial_available(user)`` is
     True, and never touches the limited trial's own button/callback_data.
     Also: ``limited_available`` independently hides/shows the limited
     "Активировать" button (reachability fix — see
     tests/test_trial_disabled_menu_gating.py for the screen/menu-entry side
     of the same fix).
  2. ``activate_unlimited_trial`` handler (app/handlers/subscription/purchase.py):
     restriction gate, eligibility gate, success UI (mirrors
     ``activate_trial``'s), and each service-layer failure mode rendered as a
     friendly message instead of propagating.
  3. Registration: ``activate_unlimited_trial`` is wired to callback_data
     ``'activate_unlimited_trial'`` inside ``register_handlers``, alongside
     ``trial_activate``.

Follows the same style as ``tests/handlers/test_sbp_recurring_handlers.py``:
real handler/keyboard code paths, ``MagicMock``/``AsyncMock`` stand-ins for
``callback``/``db_user``/``db``, ``monkeypatch`` on the exact seam each path
calls through (the service functions are looked up as module attributes at
call time, so patching ``app.services.unlimited_trial_service.<name>`` is
what actually takes effect).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.database.models import User
from app.external.artemida_api import ArtemidaAPIError
from app.services.unlimited_trial_service import (
    UnlimitedTrialActivationError,
    UnlimitedTrialNotEligible,
    UnlimitedTrialUnavailable,
)


def _keyboard_callbacks(markup) -> list[str]:
    return [btn.callback_data for row in markup.inline_keyboard for btn in row if btn.callback_data]


# --- get_trial_keyboard ---------------------------------------------------


class TestGetTrialKeyboard:
    def test_omits_unlimited_button_when_no_user_passed(self):
        """The `user` kwarg is optional, for backward compat.

        Without it, the keyboard is exactly what it was before this feature existed.
        """
        from app.keyboards.inline import get_trial_keyboard

        markup = get_trial_keyboard('ru')
        assert _keyboard_callbacks(markup) == ['trial_activate', 'back_to_menu']

    def test_shows_unlimited_button_when_available(self, monkeypatch):
        from app.keyboards.inline import get_trial_keyboard

        monkeypatch.setattr(
            'app.services.unlimited_trial_service.unlimited_trial_available',
            lambda user: True,
        )

        markup = get_trial_keyboard('ru', user=MagicMock(spec=User))

        callbacks = _keyboard_callbacks(markup)
        assert 'activate_unlimited_trial' in callbacks
        # The limited trial button + BACK stay together, unchanged, in their own row.
        assert callbacks[-2:] == ['trial_activate', 'back_to_menu']

    def test_hides_unlimited_button_when_unavailable(self, monkeypatch):
        from app.keyboards.inline import get_trial_keyboard

        monkeypatch.setattr(
            'app.services.unlimited_trial_service.unlimited_trial_available',
            lambda user: False,
        )

        markup = get_trial_keyboard('ru', user=MagicMock(spec=User))

        assert _keyboard_callbacks(markup) == ['trial_activate', 'back_to_menu']

    def test_gate_receives_the_actual_user_object(self, monkeypatch):
        """The keyboard builder must pass the real user through to the gate.

        Not a stand-in — otherwise the eligibility check is meaningless.
        """
        from app.keyboards.inline import get_trial_keyboard

        seen = []
        monkeypatch.setattr(
            'app.services.unlimited_trial_service.unlimited_trial_available',
            lambda user: seen.append(user) or True,
        )

        sentinel_user = MagicMock(spec=User)
        get_trial_keyboard('ru', user=sentinel_user)

        assert seen == [sentinel_user]

    def test_limited_available_false_hides_the_limited_button(self, monkeypatch):
        """A limited-used-but-unlimited-eligible user sees only the unlimited
        row + Back — the limited 'Активировать' must not appear at all."""
        from app.keyboards.inline import get_trial_keyboard

        monkeypatch.setattr(
            'app.services.unlimited_trial_service.unlimited_trial_available',
            lambda user: True,
        )

        markup = get_trial_keyboard('ru', user=MagicMock(spec=User), limited_available=False)

        assert _keyboard_callbacks(markup) == ['activate_unlimited_trial', 'back_to_menu']

    def test_limited_available_true_keeps_both_buttons(self, monkeypatch):
        """Explicit limited_available=True (the default) alongside an
        unlimited-eligible user shows both offers together."""
        from app.keyboards.inline import get_trial_keyboard

        monkeypatch.setattr(
            'app.services.unlimited_trial_service.unlimited_trial_available',
            lambda user: True,
        )

        markup = get_trial_keyboard('ru', user=MagicMock(spec=User), limited_available=True)

        assert _keyboard_callbacks(markup) == ['activate_unlimited_trial', 'trial_activate', 'back_to_menu']

    def test_limited_available_false_without_unlimited_still_shows_back(self):
        """Neither offer available -> not an empty keyboard, at least Back."""
        from app.keyboards.inline import get_trial_keyboard

        markup = get_trial_keyboard('ru', limited_available=False)

        assert _keyboard_callbacks(markup) == ['back_to_menu']


# --- activate_unlimited_trial handler -------------------------------------


def _make_callback():
    cb = MagicMock()
    cb.message = MagicMock()
    cb.message.edit_text = AsyncMock()
    cb.answer = AsyncMock()
    cb.bot = AsyncMock()
    return cb


def _make_user(**overrides):
    user = MagicMock(spec=User)
    user.id = 1
    user.telegram_id = 111
    user.language = 'ru'
    user.restriction_subscription = False
    for key, value in overrides.items():
        setattr(user, key, value)
    return user


def _make_texts():
    texts = MagicMock()
    texts.t = lambda key, default, **kwargs: default
    texts.BACK = 'Назад'
    texts.ERROR = 'Ошибка'
    texts.TRIAL_ACTIVATED = 'Триал активирован'
    return texts


@pytest.mark.asyncio
async def test_activate_unlimited_trial_blocks_when_restricted(monkeypatch):
    """Mirrors activate_trial's own restriction_subscription gate.

    Must fire BEFORE the eligibility check even runs.
    """
    from app.handlers.subscription import purchase

    cb = _make_callback()
    user = _make_user(restriction_subscription=True, restriction_reason='test restriction')
    db = AsyncMock()

    monkeypatch.setattr(purchase, 'get_texts', lambda language: _make_texts())
    gate = MagicMock(return_value=True)
    monkeypatch.setattr('app.services.unlimited_trial_service.unlimited_trial_available', gate)

    await purchase.activate_unlimited_trial(cb, user, db)

    cb.message.edit_text.assert_awaited_once()
    body = cb.message.edit_text.call_args[0][0]
    assert 'Активация подписки ограничена' in body
    assert 'test restriction' in body
    gate.assert_not_called()
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_activate_unlimited_trial_shows_unavailable_when_gate_fails(monkeypatch):
    from app.handlers.subscription import purchase

    cb = _make_callback()
    user = _make_user()
    db = AsyncMock()

    monkeypatch.setattr(purchase, 'get_texts', lambda language: _make_texts())
    monkeypatch.setattr('app.services.unlimited_trial_service.unlimited_trial_available', lambda u: False)
    service_activate = AsyncMock()
    monkeypatch.setattr('app.services.unlimited_trial_service.activate_unlimited_trial', service_activate)

    await purchase.activate_unlimited_trial(cb, user, db)

    # Fails fast on the handler-level gate — the service is never even called.
    service_activate.assert_not_called()
    cb.message.edit_text.assert_awaited_once()
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_activate_unlimited_trial_success_shows_connect_steps(monkeypatch):
    from app.handlers.subscription import purchase

    cb = _make_callback()
    user = _make_user()
    db = AsyncMock()

    fake_subscription = MagicMock()
    fake_subscription.subscription_url = 'https://vendor/sub/xyz'
    fake_subscription.subscription_crypto_link = None

    monkeypatch.setattr(purchase, 'get_texts', lambda language: _make_texts())
    monkeypatch.setattr('app.services.unlimited_trial_service.unlimited_trial_available', lambda u: True)
    service_activate = AsyncMock(return_value=fake_subscription)
    monkeypatch.setattr('app.services.unlimited_trial_service.activate_unlimited_trial', service_activate)

    fake_notification_service = MagicMock()
    notify = AsyncMock(return_value=True)
    fake_notification_service.send_trial_activation_notification = notify
    monkeypatch.setattr(purchase, 'AdminNotificationService', lambda bot: fake_notification_service)

    await purchase.activate_unlimited_trial(cb, user, db)

    service_activate.assert_awaited_once_with(db, user, bot=cb.bot)
    db.refresh.assert_awaited_once_with(user)
    notify.assert_awaited_once()

    cb.message.edit_text.assert_awaited_once()
    _args, kwargs = cb.message.edit_text.call_args
    assert kwargs.get('parse_mode') == 'HTML'
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
async def test_activate_unlimited_trial_success_falls_back_when_no_link(monkeypatch):
    """Falls back to the generic text when there is no subscription_url yet.

    Vendor lag case: not a crash, not an empty connect-steps screen.
    """
    from app.handlers.subscription import purchase

    cb = _make_callback()
    user = _make_user()
    db = AsyncMock()

    fake_subscription = MagicMock()
    fake_subscription.subscription_url = None
    fake_subscription.subscription_crypto_link = None

    monkeypatch.setattr(purchase, 'get_texts', lambda language: _make_texts())
    monkeypatch.setattr('app.services.unlimited_trial_service.unlimited_trial_available', lambda u: True)
    monkeypatch.setattr(
        'app.services.unlimited_trial_service.activate_unlimited_trial',
        AsyncMock(return_value=fake_subscription),
    )
    monkeypatch.setattr(purchase, 'AdminNotificationService', lambda bot: MagicMock())

    await purchase.activate_unlimited_trial(cb, user, db)

    cb.message.edit_text.assert_awaited_once()
    body = cb.message.edit_text.call_args[0][0]
    assert 'Триал активирован' in body
    cb.answer.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    'error',
    [
        UnlimitedTrialNotEligible('nope'),
        UnlimitedTrialUnavailable('nope'),
        UnlimitedTrialActivationError('nope'),
        ArtemidaAPIError('vendor down'),
        RuntimeError('boom'),
    ],
    ids=[
        'not_eligible',
        'unavailable',
        'activation_error',
        'vendor_error',
        'unexpected_error',
    ],
)
async def test_activate_unlimited_trial_handles_service_failures_gracefully(monkeypatch, error):
    """Every service failure mode must land on a normal keyboard screen.

    Never an unhandled exception bubbling out of the handler.
    """
    from app.handlers.subscription import purchase

    cb = _make_callback()
    user = _make_user()
    db = AsyncMock()

    monkeypatch.setattr(purchase, 'get_texts', lambda language: _make_texts())
    monkeypatch.setattr('app.services.unlimited_trial_service.unlimited_trial_available', lambda u: True)
    monkeypatch.setattr(
        'app.services.unlimited_trial_service.activate_unlimited_trial',
        AsyncMock(side_effect=error),
    )

    await purchase.activate_unlimited_trial(cb, user, db)

    cb.message.edit_text.assert_awaited_once()
    cb.answer.assert_awaited_once()
    _args, kwargs = cb.message.edit_text.call_args
    assert kwargs['reply_markup'] is not None


# --- registration ----------------------------------------------------------


def test_activate_unlimited_trial_is_registered_alongside_trial_activate():
    """register_handlers wires callback_data == 'activate_unlimited_trial'.

    Same router/decorator pattern as activate_trial, registered right next to it.
    """
    from app.handlers.subscription import purchase

    registered: list[tuple[tuple, object]] = []

    class _Registry:
        def register(self, handler, *conditions):
            registered.append((conditions, handler))

    class _Dispatcher:
        callback_query = _Registry()
        message = _Registry()

    purchase.register_handlers(_Dispatcher())

    by_name = {handler.__name__: conditions for conditions, handler in registered}
    assert 'activate_unlimited_trial' in by_name
    assert 'activate_trial' in by_name

    def _resolves_for(conditions, data) -> bool:
        fake_event = MagicMock()
        fake_event.data = data
        return all(bool(cond.resolve(fake_event)) for cond in conditions)

    unlimited_conditions = by_name['activate_unlimited_trial']
    limited_conditions = by_name['activate_trial']

    assert _resolves_for(unlimited_conditions, 'activate_unlimited_trial')
    assert not _resolves_for(unlimited_conditions, 'trial_activate')
    assert _resolves_for(limited_conditions, 'trial_activate')
    assert not _resolves_for(limited_conditions, 'activate_unlimited_trial')
