"""Resolve relative time expressions against the data's as-of date (IST), not the wall clock.

`resolve("last two weeks", as_of)` -> TimeRange(2026-10-14, 2026-10-27, ...)
Vague phrases ("recently") come back with `ambiguous=True` plus a suggested default,
so the pipeline can ask or disclose instead of silently guessing.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

_NUM = {"a": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
        "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "fourteen": 14, "thirty": 30, "ninety": 90}
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], start=1)}
_MONTHS.update({k[:3]: v for k, v in list(_MONTHS.items())})
_MONTHS["sept"] = 9
AMBIGUOUS = ("recently", "lately", "of late", "these days", "past few days", "last few days", "a while",
             "recent")
AMBIGUOUS_DEFAULT_DAYS = 14


@dataclass(frozen=True)
class TimeRange:
    start: date
    end: date
    label: str
    expression: str
    ambiguous: bool = False
    note: str = ""

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous_period(self) -> "TimeRange":
        """Equal-length period immediately before this one (the default comparison)."""
        end = self.start - timedelta(days=1)
        return TimeRange(end - timedelta(days=self.days - 1), end, f"previous {self.days} days",
                         f"previous period of {self.expression!r}")

    def clipped(self, lo: date, hi: date) -> "TimeRange":
        s, e = max(self.start, lo), min(self.end, hi)
        if s > e:
            raise TimeParseError(f"{self.expression!r} ({self.start}..{self.end}) is outside the data ({lo}..{hi})")
        note = self.note
        if (s, e) != (self.start, self.end):
            note = (note + " " if note else "") + f"Clipped to available data {s}..{e}."
        return TimeRange(s, e, self.label, self.expression, self.ambiguous, note)

    def as_dict(self) -> dict:
        return {"start": self.start.isoformat(), "end": self.end.isoformat(), "label": self.label,
                "expression": self.expression, "ambiguous": self.ambiguous, "note": self.note}


class TimeParseError(ValueError):
    pass


def _n(tok: str) -> int:
    return int(tok) if tok.isdigit() else _NUM[tok]


def _month_range(y: int, m: int) -> tuple[date, date]:
    start = date(y, m, 1)
    nxt = date(y + (m == 12), m % 12 + 1, 1)
    return start, nxt - timedelta(days=1)


def _quarter_range(y: int, q: int) -> tuple[date, date]:
    s = date(y, 3 * (q - 1) + 1, 1)
    return s, _month_range(y, 3 * q)[1]


def _date(tok: str, as_of: date) -> date:
    tok = tok.strip().rstrip(",")
    try:
        return date.fromisoformat(tok)
    except ValueError:
        pass
    m = re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)\.?(?:\s+(\d{4}))?", tok) or \
        re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?", tok)
    if m:
        a, b, y = m.groups()
        day, mon = (a, b) if a.isdigit() else (b, a)
        if mon in _MONTHS:
            return date(int(y) if y else as_of.year, _MONTHS[mon], int(day))
    raise TimeParseError(f"cannot read date {tok!r}")


def resolve(expression: str, as_of: date) -> TimeRange:
    """Resolve one time expression. Raises TimeParseError if it is not understood."""
    raw = expression
    s = re.sub(r"\s+", " ", expression.strip().lower())
    s = re.sub(r"^(in|during|for|over|of|the)\s+", "", s)
    s = re.sub(r"^(the|past)\s+", "last ", s) if s.startswith(("the past", "past ")) else s
    s = s.replace("previous ", "last ").replace("prior ", "last ").replace("fortnight", "2 weeks")
    s = s.replace("the last ", "last ")

    def tr(a, b, label, **kw):
        return TimeRange(a, b, label, raw, **kw)

    for phrase in AMBIGUOUS:
        if phrase in s:
            return tr(as_of - timedelta(days=AMBIGUOUS_DEFAULT_DAYS - 1), as_of,
                      f"last {AMBIGUOUS_DEFAULT_DAYS} days (assumed)", ambiguous=True,
                      note=f"{raw!r} is vague; assumed the last {AMBIGUOUS_DEFAULT_DAYS} days ending {as_of}.")

    if s in ("today", "so far today"):
        return tr(as_of, as_of, f"{as_of} (latest complete day)",
                  note="'Today' means the latest complete day in the data.")
    if s == "yesterday":
        d = as_of - timedelta(days=1)
        return tr(d, d, str(d))
    m = re.fullmatch(r"last (\d+|[a-z]+) (day|week|month)s?", s)
    if m and m.group(1) not in ("week", "month", "quarter", "year"):
        n, unit = _n(m.group(1)), m.group(2)
        if unit == "day":
            return tr(as_of - timedelta(days=n - 1), as_of, f"last {n} days")
        if unit == "week":
            return tr(as_of - timedelta(days=7 * n - 1), as_of, f"last {7 * n} days",
                      note=f"'{raw}' read as the rolling {7 * n} days ending {as_of}, not calendar weeks.")
        y, mo = divmod(as_of.year * 12 + as_of.month - 1 - n, 12)
        back = date(y, mo + 1, min(as_of.day, _month_range(y, mo + 1)[1].day))
        return tr(back + timedelta(days=1), as_of, f"last {n} months (rolling)",
                  note=f"'{raw}' read as the rolling {n} months ending {as_of}.")
    if s == "last week":
        mon = as_of - timedelta(days=as_of.weekday() + 7)
        return tr(mon, mon + timedelta(days=6), f"week of {mon} (Mon-Sun)")
    if s in ("this week", "week to date", "wtd"):
        return tr(as_of - timedelta(days=as_of.weekday()), as_of, "week to date")
    if s in ("this month", "month to date", "mtd", "current month"):
        return tr(as_of.replace(day=1), as_of, f"{as_of:%B %Y} month-to-date")
    if s == "last month":
        a, b = _month_range(*(((as_of.year - 1, 12) if as_of.month == 1 else (as_of.year, as_of.month - 1))))
        return tr(a, b, f"{a:%B %Y}")
    q_now = (as_of.month - 1) // 3 + 1
    if s in ("this quarter", "quarter to date", "qtd", "current quarter"):
        return tr(_quarter_range(as_of.year, q_now)[0], as_of, f"Q{q_now} {as_of.year} to date")
    if s == "last quarter":
        y, q = (as_of.year - 1, 4) if q_now == 1 else (as_of.year, q_now - 1)
        a, b = _quarter_range(y, q)
        return tr(a, b, f"Q{q} {y}")
    m = re.fullmatch(r"q([1-4])(?:\s*(?:fy)?\s*(\d{4}))?", s)
    if m:
        y = int(m.group(2)) if m.group(2) else as_of.year
        a, b = _quarter_range(y, int(m.group(1)))
        return tr(a, b, f"Q{m.group(1)} {y} (calendar)")
    m = re.fullmatch(r"([a-z]+)\.?(?:\s+(\d{4}))?", s)
    if m and m.group(1) in _MONTHS:
        mon = _MONTHS[m.group(1)]
        y = int(m.group(2)) if m.group(2) else (as_of.year if mon <= as_of.month else as_of.year - 1)
        a, b = _month_range(y, mon)
        return tr(a, b, f"{a:%B %Y}")
    m = re.fullmatch(r"(?:between |from )?(.+?) (?:and|to|until|till|-) (.+)", s)
    if m:
        try:
            a, b = _date(m.group(1), as_of), _date(m.group(2), as_of)
            return tr(a, b, f"{a} to {b}")
        except TimeParseError:
            pass
    m = re.fullmatch(r"(?:since|from) (.+?)(?: (?:to|until|till) (?:now|today|date))?", s)
    if m:
        d = _date(m.group(1), as_of)
        return tr(d, as_of, f"{d} to {as_of}")
    try:
        d = _date(s.removeprefix("on "), as_of)
        return tr(d, d, str(d))
    except TimeParseError:
        pass
    raise TimeParseError(f"don't understand the time expression {raw!r}")


def detect_as_of(con, ratio: float = 0.85, lookback: int = 14) -> tuple[date, dict]:
    """Last COMPLETE IST day: walk back from the latest day until its volume is at least
    `ratio` x the median of the previous `lookback` days. Returns (as_of, evidence)."""
    rows = con.execute("SELECT date_key, COUNT(*) FROM v_attempts GROUP BY 1 ORDER BY 1").fetchall()
    if not rows:
        raise TimeParseError("no data")
    keys = [r[0] for r in rows]
    counts = [r[1] for r in rows]
    skipped = []
    for i in range(len(rows) - 1, lookback - 1, -1):
        prev = sorted(counts[i - lookback:i])
        med = prev[len(prev) // 2] if len(prev) % 2 else (prev[len(prev) // 2 - 1] + prev[len(prev) // 2]) / 2
        if counts[i] >= ratio * med:
            d = date(keys[i] // 10000, keys[i] // 100 % 100, keys[i] % 100)
            return d, {"as_of": d.isoformat(), "incomplete_days": skipped,
                       "rule": f"volume >= {ratio} x median of previous {lookback} days"}
        skipped.append({"date_key": keys[i], "rows": counts[i], "ratio_to_median": round(counts[i] / med, 3)})
    k = keys[-1]
    return date(k // 10000, k // 100 % 100, k % 100), {"as_of": None, "incomplete_days": skipped,
                                                        "rule": "fallback: latest day"}
