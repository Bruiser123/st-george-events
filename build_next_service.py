#!/usr/bin/env python3
"""Build a tiny live-clock widget from the public St. George Upper Darby calendar."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from html import escape, unescape
from pathlib import Path
import re
import sys
from zoneinfo import ZoneInfoNotFoundError

from build_events import (
    BuildError, INTERNAL, NEGATIVE, TZ, UPPER_CAL, UPPER_ICS, atomic_write,
    fetch, format_clock, format_day, from_calendar, ics_text, normalize, parse_iso,
    prop, read_bounded,
)

DAYS = 14
MAX_AGE = timedelta(hours=30)
DAY_CANCEL = "whole day cancellation"
PRIVATE = re.compile(
    r"\b(?:private|confidential|invitation[ -]only|(?:staff|members|guests|family) only|"
    r"not open to (?:the )?public|closed to (?:the )?public|by appointment|"
    r"(?:online|virtual|remote)[ -]only|(?:held|meeting|join|meet) (?:only )?(?:on zoom|online)|"
    r"(?:service|liturgy|vespers|orthros|matins) (?:is|will be) (?:online|virtual|remote)|"
    r"zoom(?:\.us)?|tentative|unconfirmed|not confirmed|off[ -]site)\b", re.I
)
FREE_TEXT_CANCEL = re.compile(
    r"\b(?:service|liturgy|vespers|orthros|matins)\s+(?:(?:has been|is|will be)\s+)?"
    r"(?:canceled|cancelled|postponed)\b", re.I
)
# Output parish-owned, fixed vocabulary only, never a person or other text
# copied from SUMMARY. Unrecognized services remain on the full calendar.
NAMES: dict[str, tuple[str, str, int]] = {}
for family, entries in (
    ("orthros", ("Matins", "Orthros", "Daily Orthros", "Bridegroom Matins", "Orthros with Lamentations")),
    ("liturgy", ("Liturgy", "Divine Liturgy", "Hierarchical Divine Liturgy", "Presanctified Liturgy",
                 "Divine Liturgy of St. Basil", "Leavetaking of Pascha Liturgy")),
    ("vespers", ("Vespers", "Great Vespers", "Readers Vespers", "Family Vespers", "Agape Vespers",
                 "Forgiveness Vespers", "Pentecost Kneeling Vespers", "Vespers / Readers Vespers")),
    ("combined", ("Orthros & Liturgy", "Matins & Liturgy", "Vespers & Liturgy", "Vesperal Divine Liturgy",
                  "Rush Procession & Paschal Matins and Liturgy")),
    ("compline", ("Great Compline", "Little Compline", "Great Compline with Canon of St. Andrew",
                  "Little Compline with Canon of St. Lazarus",
                  "Little Compline with Canon of St. Andrew & Life of St. Mary of Egypt")),
    ("hours", ("First Hour", "Third Hour", "Third Hour Prayers", "Sixth Hour", "Ninth Hour", "Royal Hours")),
    ("akathist", ("Akathist",)), ("paraklesis", ("Paraklesis",)),
    ("unction", ("Holy Unction",)), ("vigil", ("Vigil", "All-Night Vigil")),
    ("water", ("Blessing of the Waters", "Great Blessing of the Waters")),
):
    for label in entries:
        priority = {"Vespers": 0, "Great Vespers": 1}.get(label, 2)
        NAMES[normalize(label)] = (label, family, priority)
NAMES["great compline w canon of st andrew"] = NAMES["great compline with canon of st andrew"]

FEASTS = (
    "Annunciation", "Ascension", "Circumcision of Christ", "Entrance of the Theotokos",
    "Epiphany", "Forgiveness Sunday", "Great and Holy Friday", "Holy Friday", "Holy Saturday",
    "Lazarus Saturday", "Meeting of Our Lord", "Midfeast", "Nativity", "Pascha", "Pentecost",
    "Saturday of Souls", "SS. Constantine & Helena", "SS. Peter & Paul", "St. George", "St. Katherine",
    "St. Nicholas", "Thanksgiving", "Theophany", "Transfiguration", "the Life Giving Spring",
    "the Three Holy Hierarchs", "the Leavetaking",
)
for form in ("Liturgy", "Divine Liturgy", "Orthros", "Matins", "Vespers", "Great Vespers",
             "Orthros & Liturgy", "Royal Hours"):
    for feast in FEASTS:
        label = f"{form} for {feast}"
        NAMES[normalize(label)] = (label, NAMES[normalize(form)][1], 3)
NAMES["great vespers for the annunciation"] = NAMES["great vespers for annunciation"]


def private_description(description: str) -> bool:
    return bool(PRIVATE.search(unescape(re.sub(r"<[^>]*>", " ", description))))


def public_service(title: str, description: str, source: str) -> bool:
    return (source == "upper" and normalize(title) in NAMES
            and not NEGATIVE.search(title) and not INTERNAL.search(title)
            and not private_description(description))


def record_marker(record, parent) -> str | None:
    for row in (record, parent):
        if prop(row, "CLASS").upper() in {"PRIVATE", "CONFIDENTIAL"} or private_description(
                ics_text(prop(row, "DESCRIPTION"))):
            return None
        place = normalize(ics_text(prop(row, "LOCATION")))
        if place and place not in {"st george orthodox church", "saint george orthodox church"} and not re.search(
                r"\b8210 (?:w|west) chester pike\b", place):
            return None
    if normalize(ics_text(prop(record, "SUMMARY"))).startswith("no "):
        # A literal "NO Vespers" is a date-level cancellation; the calendar
        # exports it at 4:30 p.m. even when a duplicate 5 p.m. series remains.
        return DAY_CANCEL
    return ""


def utc(when: datetime) -> datetime:
    if when.utcoffset() is None:
        raise BuildError("expected a datetime with an explicit UTC offset")
    return when.astimezone(timezone.utc)


def upcoming(raw: bytes, now: datetime) -> list[tuple[str, datetime]]:
    cutoff = utc(now)
    today = now.astimezone(TZ).date()
    right = today + timedelta(days=DAYS)
    rows = from_calendar(raw, "upper", today, right, publish=public_service,
                         extra_cancel=FREE_TEXT_CANCEL.search, classify_record=record_marker)
    canceled, by_slot = [], {}
    for row in rows:
        label, family, priority = NAMES[normalize(row.title)]
        start = parse_iso(row.start)
        if row.cancelled:
            canceled.append((family, start, row.note == DAY_CANCEL))
        elif isinstance(start, datetime) and today <= start.date() < right:
            key = (family, utc(start))
            previous = by_slot.get(key)
            if previous is None or (priority, label) > (previous[2], previous[0]):
                by_slot[key] = (label, start, priority)

    def suppressed(family, candidate):
        _label, start, priority = candidate
        instant = utc(start)
        for kind, when, full_day in canceled:
            if kind != family:
                continue
            timed = isinstance(when, datetime)
            if (timed and utc(when) == instant) or (
                (full_day or not timed) and (when.date() if timed else when) == start.date()
            ):
                return True
        # The older generic 5 p.m. Vespers recurrence overlaps newer 4:30 p.m.
        # Great/Family Vespers. Keep the named listing; do not switch back to
        # the stale duplicate after its replacement has started.
        return family == "vespers" and priority == 0 and any(
            k == family and other_priority > priority and other.date() == start.date()
            and abs(utc(other) - instant) <= timedelta(hours=1)
            for (k, _), (_title, other, other_priority) in by_slot.items()
        )

    return sorted(
        [(candidate[0], candidate[1]) for (kind, instant), candidate in by_slot.items()
         if instant > cutoff and not suppressed(kind, candidate)],
        key=lambda row: (utc(row[1]), row[0]),
    )


def render(services: list[tuple[str, datetime]], checked: datetime) -> str:
    rows = []
    for label, start in services:
        start = start.astimezone(TZ)
        date_and_time = f"{format_day(start.date())} at {format_clock(start)} {start:%Z}"
        rows.append(f'<li hidden data-start="{int(utc(start).timestamp()*1000)}">'
                    f'<strong>{escape(label)}</strong><br>'
                    f'<time datetime="{escape(start.isoformat(), quote=True)}">{escape(date_and_time)}</time></li>')
    items = "\n".join(rows)
    deadline = int(utc(checked).timestamp()*1000)
    limit = int(MAX_AGE.total_seconds()*1000)
    return f'''<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Next scheduled service | St. George Orthodox Church</title>
<style>
body{{box-sizing:border-box;margin:0;padding:0.65rem 0.9rem;color:#362b28;background:#fffcf6;font:1rem/1.45 Georgia,serif}}
h1{{font-size:1.15rem;margin:0 0 .3rem;color:#672d39}}
p{{margin:.35rem 0 0}}ul{{margin:0;padding:0;list-style:none}}strong{{font-size:1.1rem}}
a{{color:#672d39}}a:focus-visible{{outline:2px solid #672d39;outline-offset:2px}}[hidden]{{display:none!important}}
</style></head>
<body><main>
<h1>Next scheduled service</h1>
<div aria-live="polite">
<ul id="service-list">
{items}
</ul>
<p id="service-status">Check the parish calendar for service times.</p>
</div>
<p><a href="{UPPER_CAL}" target="_blank" rel="noopener noreferrer">Full parish calendar</a></p>
</main>
<script>
const checked = {deadline};
const limit = {limit};
const serviceRows = Array.from(document.querySelectorAll('#service-list li'));
const statusLine = document.getElementById('service-status');
let shown;
let timer;
function updateService() {{
  const now = Date.now();
  const fresh = now >= checked - 300000 && now < checked + limit;
  const next = fresh ? serviceRows.find(row => Number(row.dataset.start) > now) : undefined;
  if (next !== shown) {{
    if (shown) shown.hidden = true;
    if (next) next.hidden = false;
    shown = next;
  }}
  statusLine.hidden = Boolean(next);
  clearTimeout(timer);
  if (next) {{
    timer = setTimeout(updateService, Math.max(25, Math.min(60000, Number(next.dataset.start) - now, checked + limit - now)));
  }}
}}
document.addEventListener('visibilitychange', updateService);
window.addEventListener('pageshow', updateService);
updateService();
</script>
</body></html>
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--input", type=Path, help="Replay a local, approved St. George ICS fixture")
    parser.add_argument("--as-of", help="Override clock with explicit ISO offset for testing only")
    args = parser.parse_args()
    try:
        now = datetime.now(TZ)
        if args.as_of:
            now = parse_iso(args.as_of)
            if not isinstance(now, datetime):
                raise BuildError("--as-of requires a date, time and explicit UTC offset")
        raw = read_bounded(args.input) if args.input else fetch(UPPER_ICS)
        services = upcoming(raw, now)
        atomic_write(args.output, render(services, now))
        args.output.chmod(0o644)
    except (BuildError, OSError, ValueError, TypeError, OverflowError, AttributeError,
            KeyError, ZoneInfoNotFoundError) as exc:
        print(f"Service widget build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Generated service widget with {len(services)} upcoming service starts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
