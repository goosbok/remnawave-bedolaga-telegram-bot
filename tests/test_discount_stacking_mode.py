from app.config import settings


def test_default_mode_is_multiply(monkeypatch):
    monkeypatch.setattr(settings, 'DISCOUNT_STACKING_MODE', 'multiply', raising=False)
    assert settings.get_discount_stacking_mode() == 'multiply'
    assert settings.is_discount_stacking_max() is False


def test_max_mode(monkeypatch):
    monkeypatch.setattr(settings, 'DISCOUNT_STACKING_MODE', 'max', raising=False)
    assert settings.get_discount_stacking_mode() == 'max'
    assert settings.is_discount_stacking_max() is True


def test_invalid_value_falls_back_to_multiply(monkeypatch):
    monkeypatch.setattr(settings, 'DISCOUNT_STACKING_MODE', 'bogus', raising=False)
    assert settings.get_discount_stacking_mode() == 'multiply'
    assert settings.is_discount_stacking_max() is False
