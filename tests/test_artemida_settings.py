"""ARTΞMIDA vendor API settings on ``Settings``.

Later tasks add an aiohttp client and a rebrand endpoint that read these
fields off ``app.config.settings``. This test only pins that the fields
exist, read overrides from env/kwargs, and default to disabled — following
the repo's established pattern of instantiating a fresh ``Settings(...)``
directly (see ``tests/database/test_pool_config.py`` and
``tests/cabinet/test_info_service.py``) rather than reloading the module,
which is fragile against the module-level ``settings`` singleton.
"""

from app.config import Settings


def test_artemida_settings_defaults_are_disabled():
    """Vendor integration must ship off by default."""
    config = Settings(BOT_TOKEN='1:test')

    assert config.ARTEMIDA_ENABLED is False
    assert config.ARTEMIDA_BASE_URL == 'https://artemida.cc/v1'
    assert config.ARTEMIDA_API_KEY is None
    assert config.ARTEMIDA_REBRAND_BASE_URL == ''
    assert config.ARTEMIDA_BRAND_TITLE == 'MAX VPN'
    assert config.ARTEMIDA_BRAND_SUPPORT_URL == ''


def test_artemida_settings_read_overrides():
    """Overrides (as env would supply as strings) are applied as-is."""
    config = Settings(
        BOT_TOKEN='1:test',
        ARTEMIDA_ENABLED=True,
        ARTEMIDA_BASE_URL='https://artemida.cc/v1',
        ARTEMIDA_API_KEY='artm_live_test',
        ARTEMIDA_REBRAND_BASE_URL='https://sub.max-vpn.online/a',
        ARTEMIDA_BRAND_TITLE='MAX VPN Custom',
        ARTEMIDA_BRAND_SUPPORT_URL='https://t.me/MaxSupport2',
    )

    assert config.ARTEMIDA_ENABLED is True
    assert config.ARTEMIDA_BASE_URL == 'https://artemida.cc/v1'
    assert config.ARTEMIDA_API_KEY == 'artm_live_test'
    assert config.ARTEMIDA_REBRAND_BASE_URL == 'https://sub.max-vpn.online/a'
    assert config.ARTEMIDA_BRAND_TITLE == 'MAX VPN Custom'
    assert config.ARTEMIDA_BRAND_SUPPORT_URL == 'https://t.me/MaxSupport2'
