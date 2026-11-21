from datetime import datetime

from agentboard.store import parse_time


def test_now_and_same_minute_mean_today():
    base = datetime(2026, 11, 21, 2, 14, 30).timestamp()
    assert parse_time("now", base) == base
    assert parse_time("02:14", base) == datetime(2026, 11, 21, 2, 14).timestamp()


def test_past_minute_rolls_to_tomorrow():
    base = datetime(2026, 11, 21, 2, 14, 30).timestamp()
    assert parse_time("02:13", base) == datetime(2026, 11, 22, 2, 13).timestamp()
