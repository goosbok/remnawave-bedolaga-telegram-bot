"""Unlimited-trial (Artemida) settings on ``Settings``.

Two settings gate the unlimited trial issued through the ARTΞMIDA vendor:
whether it is enabled at all, and which tariff id it should be granted
against. This test only pins that the fields exist, read overrides from
env/kwargs, and default to disabled — following the repo's established
pattern of instantiating a fresh ``Settings(...)`` directly (see
``tests/test_artemida_settings.py``) rather than reloading the module,
which is fragile against the module-level ``settings`` singleton.
"""

from app.config import Settings


def test_unlimited_trial_settings_default_off():
    config = Settings(BOT_TOKEN='1:test')
    assert config.ARTEMIDA_TRIAL_ENABLED is False
    assert config.ARTEMIDA_TRIAL_TARIFF_ID == 0


def test_unlimited_trial_settings_overrides():
    config = Settings(BOT_TOKEN='1:test', ARTEMIDA_TRIAL_ENABLED=True, ARTEMIDA_TRIAL_TARIFF_ID=42)
    assert config.ARTEMIDA_TRIAL_ENABLED is True
    assert config.ARTEMIDA_TRIAL_TARIFF_ID == 42
