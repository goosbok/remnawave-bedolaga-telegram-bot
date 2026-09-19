from app.database.models import Subscription, generate_public_token


def test_generate_public_token_is_urlsafe_and_unique():
    a = generate_public_token()
    b = generate_public_token()
    assert isinstance(a, str) and len(a) >= 20
    assert a != b
    # URL-safe alphabet only
    import string

    allowed = set(string.ascii_letters + string.digits + '-_')
    assert set(a) <= allowed


def test_subscription_public_token_defaults_none():
    s = Subscription(user_id=1, end_date=None)
    assert s.public_token is None
