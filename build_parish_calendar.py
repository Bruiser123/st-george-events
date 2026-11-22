#!/usr/bin/env python3
"""Render upcoming public activities from the St. George Upper Darby calendar only."""
from __future__ import annotations

import argparse
import html
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfoNotFoundError

from build_events import (
    BuildError, INTERNAL, LOCAL_NEWS, NEGATIVE, REGIONAL_NEWS, ROUTINE_COFFEE,
    TZ, UPPER_CAL, UPPER_ICS, WORSHIP, Event, atomic_write, format_day,
    format_interval, from_calendar, normalize, read_bounded, reconcile, fetch,
)

# Generic weekly listings are omitted even if a calendar admin created them as
# individual events. Thanksgiving Liturgy is also a regular annual schedule
# entry, not part of this featured-events listing. The full calendar is linked.
ROUTINE = {
    "akathist", "akathist online", "confession", "confessions", "daily orthros",
    "divine liturgy", "great vespers", "liturgy", "liturgy for thanksgiving", "matins", "orthros",
    "sunday school", "third hour", "third hour prayers", "vespers",
}
NONPUBLIC = re.compile(
    r"\b(?:parish meeting|invited (?:guests|members) only|members only|"
    r"not open to the public|internal only|staff only|volunteer roster|youth choir singing liturgy)\b", re.I
)
PRIVATE_NOTE = re.compile(
    r"\b(?:private (?:event|service|function|ceremony)|attendees only|"
    r"invitation[ -]only|not open to the public|(?:staff|members) only|"
    r"tentative|not confirmed)\b", re.I
)
CANCEL_ANYWHERE = re.compile(r"\b(?:event|service|liturgy|class|meeting)\s+(?:has been|is)\s+(?:canceled|cancelled|postponed)\b", re.I)
CONTACT = re.compile(r"https?://|\bwww\.|\S+@\S+|\b\d{3}[- .]\d{3}[- .]\d{4}\b", re.I)


def publish_event(title: str, desc: str, source: str) -> bool:
    title = " ".join(title.split())
    return bool(
        source == "upper"
        and title
        and normalize(title) not in ROUTINE
        and not NEGATIVE.search(title)
        and not INTERNAL.search(title)
        and not NONPUBLIC.search(title)
        and not PRIVATE_NOTE.search(desc)
        and not CONTACT.search(title)
        and not ROUTINE_COFFEE.fullmatch(title)
        and (WORSHIP.search(title) or LOCAL_NEWS.search(title) or REGIONAL_NEWS.search(title))
    )


def upcoming(raw: bytes, today: date, days: int = 90) -> list[Event]:
    if not 1 <= days <= 366:
        raise BuildError("look-ahead must be 1 to 366 days")
    rows = from_calendar(raw, "upper", today, today + timedelta(days=days), publish=publish_event, extra_cancel=CANCEL_ANYWHERE.search)
    # Keep cancellation records until reconciliation can suppress matching
    # duplicate VEVENTs. Descriptions, locations, and UIDs never reach output.
    return reconcile(rows, today, days, days)


def render(events: list[Event], today: date) -> str:
    esc = html.escape
    fallback = (
        f'<a href="{UPPER_CAL}" target="_blank" rel="noopener">Full parish calendar</a>'
    )
    lines = [
        '<!doctype html>',
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<title>Upcoming Parish Events | St. George Orthodox Church</title>',
        '<style>',
        'body{margin:0;padding:1rem;color:#362b28;background:#fffcf6;font:1rem/1.55 Georgia,serif}',
        'h1,h2,a{color:#672d39}h1{font-size:1.55rem;margin:.1rem 0 .5rem}',
        'h2{font-size:1.18rem;margin:1.3rem 0 .5rem}p{margin:.5rem 0}',
        'ul{list-style:none;padding:0;margin:0}li{padding:.7rem 0;border-bottom:1px solid #e0d1b8}',
        'a:focus-visible{outline:2px solid #672d39}time{display:block}',
        '</style></head>',
        f'<body data-refreshed="{today.isoformat()}"><main>',
        '<h1>Upcoming Parish Events</h1>',
        '<p>Selected services and activities at St. George Orthodox Church, Upper Darby. Times are Eastern.</p>',
        f'<p>{fallback} (including regular service times)</p>',
        '<div id="parish-events-list">',
    ]
    if not events:
        lines.append('<p>No featured events are listed for this period. Check the full calendar for the regular schedule and updates.</p>')
    month = None
    for event in events:
        current_month = event.start[:7]
        if current_month != month:
            if month is not None:
                lines.append('</ul>')
            month = current_month
            label = date.fromisoformat(month + "-01").strftime("%B %Y")
            lines.extend([f'<h2>{esc(label)}</h2>', '<ul>'])
        lines.append(f'<li><strong>{esc(event.title)}</strong>'
                     f'<time datetime="{esc(event.start, quote=True)}">{esc(format_interval(event))}</time></li>')
    if month is not None:
        lines.append('</ul>')
    lines.extend([
        '</div>',
        '<p id="parish-events-stale" hidden>This listing may be out of date. Please check the full parish calendar above.</p>',
        f'<p><small>Calendar checked {esc(format_day(today))}.</small></p>',
        '</main><script>',
        'const formatter = new Intl.DateTimeFormat("en-CA", {timeZone:"America/New_York", year:"numeric", month:"2-digit", day:"2-digit"});',
        'const parts = Object.fromEntries(formatter.formatToParts().map(({type,value})=>[type,value]));',
        'const today = Date.parse(parts.year+"-"+parts.month+"-"+parts.day);',
        'if (today - Date.parse(document.body.dataset.refreshed) >= 3 * 86400000) {',
        '  document.getElementById("parish-events-list").hidden = true;',
        '  document.getElementById("parish-events-stale").hidden = false;',
        '}',
        '</script></body></html>',
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--input", type=Path, help="Read a local St. George ICS fixture instead of fetching")
    parser.add_argument("--as-of", help="Override America/New_York date for testing")
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()
    try:
        today = date.fromisoformat(args.as_of) if args.as_of else datetime.now(TZ).date()
        raw = read_bounded(args.input) if args.input else fetch(UPPER_ICS)
        events = upcoming(raw, today, args.days)
        atomic_write(args.output, render(events, today))
        args.output.chmod(0o644)
    except (BuildError, OSError, ValueError, TypeError, OverflowError, AttributeError,
            KeyError, ZoneInfoNotFoundError) as exc:
        print(f"Parish calendar build failed: {exc}", file=sys.stderr)
        return 1
    print(f"Generated {len(events)} public parish events in {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
