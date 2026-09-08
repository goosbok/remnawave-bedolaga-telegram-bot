import os
import sys
from datetime import UTC, datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

os.environ.setdefault('BOT_TOKEN', 'test-token')

from app.services.payment_search_service import SearchParams, expand_date_to_end_of_day


def test_expand_date_to_end_of_day_pushes_bare_date_to_23_59_59():
    bare_date = datetime(2026, 10, 30, tzinfo=UTC)

    expanded = expand_date_to_end_of_day(bare_date)

    assert expanded.date() == bare_date.date()
    assert expanded.hour == 23
    assert expanded.minute == 59
    assert expanded.second == 59


def test_expand_date_to_end_of_day_leaves_explicit_time_untouched():
    with_time = datetime(2026, 10, 30, 14, 30, tzinfo=UTC)

    assert expand_date_to_end_of_day(with_time) == with_time


def test_expand_date_to_end_of_day_passes_through_none():
    assert expand_date_to_end_of_day(None) is None


def test_custom_range_upper_bound_covers_the_selected_end_day():
    """Regression test: a payment made on the 'to' day must be included.

    Before the fix, a bare 'YYYY-MM-DD' end date parsed to midnight and the
    inclusive `<=` filter excluded almost every payment made that day.
    """
    date_from = datetime(2026, 9, 20, tzinfo=UTC)
    date_to = expand_date_to_end_of_day(datetime(2026, 9, 20, tzinfo=UTC))
    params = SearchParams(date_from=date_from, date_to=date_to)

    payment_made_that_afternoon = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)

    assert params.cutoff <= payment_made_that_afternoon <= params.upper_bound
