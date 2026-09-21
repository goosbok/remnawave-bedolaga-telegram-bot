from types import SimpleNamespace

from app.database.models import User


def _sub(*, is_trial, external_provider=None, status='active'):
    s = SimpleNamespace(is_trial=is_trial, external_provider=external_provider, status=status)
    s.is_pending_trial = status == 'pending' and is_trial
    return s


def _user(subs, paid=False):
    u = User()
    u.has_had_paid_subscription = paid
    # `subscriptions` is a SQLAlchemy relationship: assigning through the normal
    # descriptor tries to instrument each item as a mapped instance, which the
    # plain SimpleNamespace stubs above are not. Poking `__dict__` directly stores
    # the value where the descriptor reads it (SQLAlchemy's InstanceState.dict is
    # the instance's own __dict__) without going through collection instrumentation.
    u.__dict__['subscriptions'] = subs
    return u


def test_no_subs_both_trials_available():
    u = _user([])
    assert u.has_used_trial('limited') is False
    assert u.has_used_trial('unlimited') is False


def test_limited_trial_used_unlimited_still_available():
    u = _user([_sub(is_trial=True, external_provider=None)])
    assert u.has_used_trial('limited') is True
    assert u.has_used_trial('unlimited') is False


def test_unlimited_trial_used_limited_still_available():
    u = _user([_sub(is_trial=True, external_provider='artemida')])
    assert u.has_used_trial('unlimited') is True
    assert u.has_used_trial('limited') is False


def test_non_artemida_external_provider_also_counts_as_unlimited():
    """A trial served by ANY external vendor is the 'unlimited' kind, not just
    artemida — otherwise swapping a trial subscription to a different vendor would
    flip it back to 'limited' and let the user re-claim the limited trial too."""
    u = _user([_sub(is_trial=True, external_provider='vendor2')])
    assert u.has_used_trial('unlimited') is True
    assert u.has_used_trial('limited') is False


def test_paid_subscription_blocks_both():
    u = _user([_sub(is_trial=False, external_provider=None)])
    assert u.has_used_trial('limited') is True
    assert u.has_used_trial('unlimited') is True


def test_paid_history_flag_blocks_both():
    u = _user([], paid=True)
    assert u.has_used_trial('limited') is True
    assert u.has_used_trial('unlimited') is True


def test_pending_trial_ignored():
    u = _user([_sub(is_trial=True, external_provider='artemida', status='pending')])
    assert u.has_used_trial('unlimited') is False


def test_is_trial_already_used_is_limited_wrapper():
    u = _user([_sub(is_trial=True, external_provider='artemida')])
    assert u.is_trial_already_used() is False
