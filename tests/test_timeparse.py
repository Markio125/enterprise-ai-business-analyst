from datetime import date

import pytest

from analyst.timeparse import TimeParseError, resolve

AS_OF = date(2026, 10, 27)   # a Tuesday


@pytest.mark.parametrize("expr,start,end", [
    ("today", "2026-10-27", "2026-10-27"),
    ("yesterday", "2026-10-26", "2026-10-26"),
    ("last 7 days", "2026-10-21", "2026-10-27"),
    ("past 30 days", "2026-09-28", "2026-10-27"),
    ("last two weeks", "2026-10-14", "2026-10-27"),
    ("over the last 2 weeks", "2026-10-14", "2026-10-27"),
    ("last fortnight", "2026-10-14", "2026-10-27"),
    ("last week", "2026-10-19", "2026-10-25"),
    ("this week", "2026-10-26", "2026-10-27"),
    ("this month", "2026-10-01", "2026-10-27"),
    ("MTD", "2026-10-01", "2026-10-27"),
    ("last month", "2026-09-01", "2026-09-30"),
    ("previous month", "2026-09-01", "2026-09-30"),
    ("last 3 months", "2026-07-28", "2026-10-27"),
    ("this quarter", "2026-10-01", "2026-10-27"),
    ("last quarter", "2026-07-01", "2026-09-30"),
    ("Q3", "2026-07-01", "2026-09-30"),
    ("q3 2026", "2026-07-01", "2026-09-30"),
    ("September", "2026-09-01", "2026-09-30"),
    ("in Aug 2026", "2026-08-01", "2026-08-31"),
    ("since 2026-10-01", "2026-10-01", "2026-10-27"),
    ("since 14 Oct", "2026-10-14", "2026-10-27"),
    ("2026-09-10", "2026-09-10", "2026-09-10"),
    ("on Sep 10", "2026-09-10", "2026-09-10"),
    ("between 2026-09-20 and 2026-09-27", "2026-09-20", "2026-09-27"),
    ("from 10 Aug to 16 Aug", "2026-08-10", "2026-08-16"),
])
def test_resolve(expr, start, end):
    tr = resolve(expr, AS_OF)
    assert (tr.start.isoformat(), tr.end.isoformat()) == (start, end)
    assert not tr.ambiguous


def test_month_name_after_as_of_means_last_year():
    assert resolve("December", AS_OF).start == date(2025, 12, 1)


@pytest.mark.parametrize("expr", ["recently", "lately", "in the past few days", "recent weeks"])
def test_ambiguous_returns_default_and_flag(expr):
    tr = resolve(expr, AS_OF)
    assert tr.ambiguous and tr.days == 14 and tr.end == AS_OF and tr.note


def test_previous_period_is_adjacent_and_equal_length():
    tr = resolve("last two weeks", AS_OF)
    p = tr.previous_period()
    assert (p.start, p.end, p.days) == (date(2026, 9, 30), date(2026, 10, 13), 14)


def test_rolling_weeks_note_discloses_interpretation():
    assert "rolling" in resolve("last 2 weeks", AS_OF).note


def test_clip_to_data_window():
    tr = resolve("last 3 months", AS_OF).clipped(date(2026, 8, 1), AS_OF)
    assert tr.start == date(2026, 8, 1) and "Clipped" in tr.note
    with pytest.raises(TimeParseError):
        resolve("Q1", AS_OF).clipped(date(2026, 7, 1), AS_OF)


@pytest.mark.parametrize("expr", ["the other day", "blue moon", "next tuesday-ish"])
def test_unknown_raises(expr):
    with pytest.raises(TimeParseError):
        resolve(expr, AS_OF)
