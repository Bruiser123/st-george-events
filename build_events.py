#!/usr/bin/env python3
"""Build a local, reviewable St. George news listing; never publishes anything."""
from __future__ import annotations

import argparse
import calendar
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    from dateutil.rrule import rrulestr
except ImportError:
    rrulestr = None

TZ = ZoneInfo("America/New_York")
UPPER = "https://www.saintgeorgeupperdarby.org"
UPPER_CAL = UPPER + "/calendar.html"
NEWS = UPPER + "/newsletters.html"
UPPER_ICS = ("https://calendar.google.com/calendar/ical/"
             "c_7stha6u2ahhgvlvefpvic1n1t4%40group.calendar.google.com/public/basic.ics")
EMMAUS = "https://www.saintpaulemmaus.org/"
EMMAUS_ICS = ("https://calendar.google.com/calendar/ical/"
              "26659412gl6kaqge7c2g0vs1sg%40group.calendar.google.com/public/basic.ics")
DIOCESE = "https://www.antiochianevents.com/"
DIOCESE_DETAIL = DIOCESE + "new-york"
PDF_PATH = re.compile(r"/uploads/6/7/8/7/6787905/[a-z0-9_-]{1,160}\.pdf\Z", re.I)
PDF_MAX = 20 * 1024**2
TEXT_MAX = 3 * 1024**2
LOOKBACK = timedelta(days=366)
DELTA = timedelta(days=1)
CLOCK = [calendar.day_name[i] for i in range(7)]
MONTHS = {v.lower(): i for i, v in enumerate(calendar.month_name) if i}
MONTH_LIST = "|".join(calendar.month_name[i] for i in range(1, 13))
WEEKDAY_LIST = "|".join(CLOCK)
PUBLIC_SOURCE = {
    "upper": "St. George calendar", "emmaus": "St. Paul, Emmaus",
    "bulletin": "The Deliverer newsletter", "diocese": "Diocesan conference",
    "reviewed_local": "Verified parish announcement",
    "reviewed_regional": "Verified regional announcement",
}
RANK = {"reviewed_local": 0, "reviewed_regional": 0, "upper": 1, "diocese": 1,
        "emmaus": 2, "bulletin": 3}
SAFE_ST_GEORGE_EVENT_LINKS = {UPPER_CAL, NEWS, UPPER + "/events.html",
                            UPPER + "/holiday-gift-drive.html", UPPER + "/sunday-school.html"}
NEGATIVE = re.compile(r"\b(?:cancel(?:led|ed|lation|s)?|tbd|tentative|postponed|"
                      r"to be determined|rain date|snow date|if needed|not confirmed)\b|"
                      r"^\s*(?:no\b|busy\s*$|private\s*$)", re.I)
INTERNAL = re.compile(r"\b(?:rehearsal|practice|council|work meeting|"
                      r"closed meeting|committee|board meeting|private|invitation.only|"
                      r"memorial|funeral|baptism|wedding|attendees only)\b", re.I)
WORSHIP = re.compile(r"\b(?:liturgy|vespers|matins|orthros|akathist|paraklesis|"
                     r"compline|orthros|worship|orthodox service|prayer service|"
                     r"prayers|prayer|benediction|confessions?|divine office|"
                     r"presanctified|moleben|typika|communion|chrismation|"
                     r"blessing of (?:water|waters|the waters)|morning office|"
                     r"liturgical service|paschal service|holy unction|doxology|"
                     r"great canon|royal hours|triduum|rite of baptism|vigil)\b", re.I)
REGIONAL_NEWS = re.compile(r"\b(?:retreat|workshop|lecture|speaker|guest talk|public talk|"
                           r"keynote|presentation|seminar|series of talks|discussion panel|"
                           r"festival|conference|convention|benefit|fundraiser|"
                           r"food drive|gift drive|coat drive|clean[ -]?up|outreach|"
                           r"service project|volunteer(?:ing| day)?|"
                           r"family (?:night|day|picnic|potluck|program)|"
                           r"community (?:picnic|dinner|potluck|meal|celebration)|"
                           r"parish picnic|open house|camp(?:out)?|"
                           r"concert|cultural (?:night|fair|program)|"
                           r"youth (?:night|rally|program|conference)|"
                           r"trunk[ -]or[ -]treat|christmas market)\b", re.I)
LOCAL_NEWS = re.compile(r"\b(?:pageant|potluck|coffee hour|soup contest|"
                        r"souper bowl|luncheon|chili cook[ -]?off|brunch|"
                        r"bishops? (?:pastoral )?visit|bp\.?\s+thomas visit|"
                        r"orthodoxy 101|parishwide trip|parish (?:outing|meeting)|"
                        r"family vespers|theophany|annunciation|forgiveness vespers|"
                        r"holy unction|good friday|holy friday|pascha|holy week|"
                        r"hierarchical divine liturgy|liturgy for)\b", re.I)
ROUTINE_COFFEE = re.compile(
    r"^(?:coffee hour|coffee hour(?:\s*[:–-]| for\b| team\b| group\b| \().*)$", re.I
)
REGIONAL_ONLINE_ONLY = re.compile(r"\b(?:online only|livestream only|virtual only)\b", re.I)
LOCAL_ALIASES = {
    "christmas pageant": "nativity pageant",
    "hopephl holiday drive": "hopephl holiday gift drive",
    "hopephl holiday gift drive deadline": "hopephl holiday gift drive",
    "st nicholas day special coffee hour": "st nicholas day coffee hour",
    "soup contest souper bowl of caring": "souper bowl of caring",
    "soup contest food drive souper bowl of caring": "souper bowl of caring",
    "family minstry potluck": "family ministry potluck",
}
KNOWN_SOURCE_NAMES = {"upper": "st. george orthodox church", "emmaus": "st. paul orthodox church"}
ICS_FIELDS = set("UID DTSTART DTEND DURATION RRULE RDATE EXDATE EXRULE RECURRENCE-ID "
                 "SEQUENCE DTSTAMP SUMMARY DESCRIPTION LOCATION STATUS CLASS".split())


class BuildError(Exception):
    """External source failed validation; do not replace the previous output."""


@dataclass(frozen=True)
class Event:
    title: str
    start: str
    end: str | None
    source: str
    source_url: str
    group: str
    location: str
    note: str = ""
    cancelled: bool = False


def normalize(s: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", unicodedata.normalize("NFKC", s).casefold()).split())


def event_key(event: Event) -> tuple[str, str]:
    name = normalize(event.title)
    if event.group == "local":
        name = LOCAL_ALIASES.get(name, name)
    if event.group == "regional" and (
        name == "parish life conference" or name == "diocesan parish life conference"
        or name.startswith("eastern dioceses parish life conference")
    ):
        name = "eastern dioceses parish life conference"
    return event.group, name


def clean(s: str, maximum: int = 160) -> str:
    s = " ".join(s.split())
    if len(s) > maximum or any(unicodedata.category(c) in {"Cc", "Cf"} for c in s):
        raise BuildError("text field too long or contains unsafe controls")
    return s


def safe_pdf(url: str) -> bool:
    p = urlsplit(url)
    return p.scheme == "https" and p.netloc == "www.saintgeorgeupperdarby.org" and not (
        p.query or p.fragment
    ) and bool(PDF_PATH.fullmatch(p.path))


def safe_review_url(url: str, group: str) -> bool:
    if group == "local":
        return safe_pdf(url) or url in SAFE_ST_GEORGE_EVENT_LINKS
    p = urlsplit(url)
    return (p.scheme == "https" and p.netloc in {
        "www.saintpaulemmaus.org", "www.antiochianevents.com", "www.antiochian.org",
        "st-philip.net", "www.st-philip.net",
    } and not (p.query or p.fragment)
            and bool(re.fullmatch(r"/[a-zA-Z0-9/_-]{0,190}", p.path))
            and "//" not in p.path)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Exact endpoints should be stable. Don't follow a redirect (including to sign-in).
        raise BuildError("official endpoint redirected; check the approved URL")


def fetch(url: str, *, limit: int = TEXT_MAX) -> bytes:
    if url not in {UPPER_ICS, EMMAUS_ICS, NEWS, DIOCESE} and not safe_pdf(url):
        raise BuildError("endpoint is not in the source allowlist")
    try:
        r = Request(url, headers={"User-Agent": "StGeorgePublicNews/1.0"})
        with build_opener(NoRedirect()).open(r, timeout=12) as response:
            data = response.read(limit + 1)
    except (URLError, OSError, TimeoutError) as exc:
        raise BuildError(f"official source fetch failed: {type(exc).__name__}: {exc}") from exc
    if not data or len(data) > limit:
        raise BuildError("official source response is empty or exceeds its size limit")
    return data


def ics_text(text: str) -> str:
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1].lower() == "n" else m[1], text)


def day(value: date | datetime) -> date:
    return value.astimezone(TZ).date() if isinstance(value, datetime) else value


def iso(value: date | datetime) -> str:
    return value.astimezone(TZ).isoformat() if isinstance(value, datetime) else value.isoformat()


def parse_iso(value: str) -> date | datetime:
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return date.fromisoformat(value)
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:\d{2})", value):
        raise BuildError("expected ISO local date or date and explicit UTC offset")
    return datetime.fromisoformat(value).astimezone(TZ)


def end_day(event: Event) -> date:
    start = parse_iso(event.start)
    if event.end is None:
        return day(start)
    end = parse_iso(event.end)
    if not isinstance(start, datetime):
        return day(end) - DELTA                 # RFC 5545 exclusive all-day end.
    if isinstance(end, datetime) and end > start and end.timetz().replace(tzinfo=None) == time.min:
        return day(end) - DELTA
    return day(end)


def in_window(start: date | datetime, end: date | datetime | None, left: date, right: date) -> bool:
    if day(start) >= right:
        return False
    if day(start) >= left:
        return True
    return end is not None and (day(end) > left or (
        isinstance(end, datetime) and day(end) == left and end.timetz().replace(tzinfo=None) != time.min
    ))


def can_publish(title: str, desc: str, source: str) -> bool:
    if NEGATIVE.search(title) or INTERNAL.search(title):
        return False
    if source == "upper":
        return bool((REGIONAL_NEWS.search(title) or LOCAL_NEWS.search(title))
                    and not ROUTINE_COFFEE.fullmatch(title))
    # No other parish's worship, or a combined program, even if otherwise a qualifying event.
    if WORSHIP.search(title) or WORSHIP.search(desc) or REGIONAL_ONLINE_ONLY.search(title + " " + desc):
        return False
    return bool(REGIONAL_NEWS.search(title))


def cancellation_stem(title: str) -> str:
    return clean(re.sub(r"^\s*(?:cancel(?:led|ed)|postponed|no)\b[\s:–—-]*|"
                        r"\s*[-–—:(]?\s*(?:cancel(?:led|ed)|postponed)\s*\)?\s*$",
                        "", title, flags=re.I))


def checked_duration(start: date | datetime, end: date | datetime | None):
    if end is None:
        return None
    if type(start) is not type(end) or end < start or day(end) - day(start) > LOOKBACK:
        raise BuildError("invalid or excessively long event end")
    if end == start and not isinstance(start, datetime):
        raise BuildError("all-day end must be after start")
    return end


def from_ics_value(value: str, params: dict[str, str]) -> date | datetime:
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return datetime.strptime(value, "%Y%m%d").date()
    m = re.fullmatch(r"(\d{8}T\d{6})(Z?)", value)
    if not m:
        raise BuildError("unrecognized calendar date/time")
    z = timezone.utc if m[2] else ZoneInfo(params.get("TZID", TZ.key))
    return datetime.strptime(m[1], "%Y%m%dT%H%M%S").replace(tzinfo=z)


def parse_ics(data: bytes, source: str):
    decoded = data.decode("utf-8-sig")
    logical = re.sub(r"\r?\n[ \t]", "", decoded.replace("\r\n", "\n")).split("\n")
    nonempty = [ln for ln in logical if ln]
    if not nonempty or nonempty[0] != "BEGIN:VCALENDAR" or nonempty[-1] != "END:VCALENDAR":
        raise BuildError(f"{source}: not a complete VCALENDAR")
    result, stack = [], []
    current, signature, version = None, "", ""
    for line in nonempty:
        if line.startswith("BEGIN:"):
            stack.append(line[6:])
            if stack == ["VCALENDAR", "VEVENT"]:
                current = defaultdict(list)
            continue
        if line.startswith("END:"):
            if not stack or stack[-1] != line[4:]:
                raise BuildError(f"{source}: malformed calendar component")
            if stack == ["VCALENDAR", "VEVENT"]:
                result.append(current)
                current = None
            stack.pop()
            continue
        if stack == ["VCALENDAR"]:
            if line.startswith("X-WR-CALNAME:"):
                signature = ics_text(line.split(":", 1)[1]).strip().lower()
            if line.startswith("VERSION:"):
                version = line.split(":", 1)[1]
            continue
        if stack != ["VCALENDAR", "VEVENT"]:
            continue
        # A quoted TZID is accepted. Parameters irrelevant to the schedule are ignored.
        m = re.match(r'^([A-Z][A-Z0-9-]*)((?:;[^:"\n]*(?:"[^"\n]*")?)*)\s*:(.*)$', line)
        if not m or m[1] not in ICS_FIELDS:
            continue
        params = {}
        for pm in re.finditer(r';([A-Z-]+)=(?:"([^"]*)"|([^;]*))', m[2]):
            params[pm[1]] = pm[2] if pm[2] is not None else pm[3]
        current[m[1]].append((params, m[3]))
    if stack or version != "2.0" or not signature.startswith(KNOWN_SOURCE_NAMES[source]):
        raise BuildError(f"{source}: wrong or incomplete public calendar")
    if not result:
        raise BuildError(f"{source}: calendar unexpectedly empty")
    return result


def prop(record, key: str, default: str = "") -> str:
    return record[key][-1][1] if record and record.get(key) else default


def dtprop(record, key: str, default=None):
    if not record or not record.get(key):
        return default
    params, value = record[key][-1]
    return from_ics_value(value, params)


def list_dates(record, key: str):
    for params, values in record.get(key, []):
        for value in values.split(","):
            if "/" in value or params.get("VALUE") == "PERIOD":
                raise BuildError("period-valued recurrence date not supported")
            yield from_ics_value(value, params)


def clock_key(when: date | datetime) -> str:
    return when.astimezone(timezone.utc).isoformat() if isinstance(when, datetime) else when.isoformat()


def span(record, start: date | datetime):
    end = dtprop(record, "DTEND")
    duration = prop(record, "DURATION")
    if end is not None:
        checked_duration(start, end)
        return end - start
    if duration:
        match = re.fullmatch(r"P(?:(\d+)W|(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?)", duration)
        if not match or not any(match.groups()):
            raise BuildError("unrecognized event duration")
        weeks, days, hours, minutes, seconds = [int(v or 0) for v in match.groups()]
        if not isinstance(start, datetime) and (hours or minutes or seconds):
            raise BuildError("all-day duration cannot use clock units")
        interval = timedelta(weeks=weeks, days=days, hours=hours, minutes=minutes, seconds=seconds)
        checked_duration(start, start + interval)
        return interval
    return DELTA if not isinstance(start, datetime) else None


def recurrence(record, start, left: date, right: date):
    if record.get("EXRULE"):
        raise BuildError("EXRULE requires manual review")
    all_day = not isinstance(start, datetime)
    pivot = datetime.combine(start, time.min) if all_day else start
    low = datetime.combine(left - LOOKBACK, time.min, pivot.tzinfo)
    high = datetime.combine(right + timedelta(days=2), time.min, pivot.tzinfo)
    dates = {clock_key(v): v for v in (start, *list_dates(record, "RDATE"))}
    for _, rule in record.get("RRULE", []):
        if rrulestr is None:
            raise BuildError("python-dateutil is required for recurring calendar events")
        # Calendar inputs cannot cause second/minute recurrence or unbounded output.
        pieces = dict(item.split("=", 1) for item in rule.split(";"))
        if pieces.get("FREQ") not in {"DAILY", "WEEKLY", "MONTHLY", "YEARLY"}:
            raise BuildError("unsupported calendar recurrence frequency")
        if not set(pieces) <= {"FREQ", "COUNT", "INTERVAL", "BYDAY", "BYMONTH",
                                "BYMONTHDAY", "BYSETPOS", "WKST", "UNTIL"}:
            raise BuildError("unsupported recurrence rule field")
        if any(int(pieces.get(k, 1)) not in range(1, 10001) for k in ("COUNT", "INTERVAL")):
            raise BuildError("excessive recurrence count or interval")
        # Two historical Google Calendar rules (and potentially future ones)
        # use a DATE UNTIL with a timed DTSTART. Treat the printed date as
        # inclusive in the event's zone; dateutil requires the resulting UTC.
        until = pieces.get("UNTIL", "")
        if pivot.tzinfo and re.fullmatch(r"\d{8}", until):
            cutoff = datetime.combine(datetime.strptime(until, "%Y%m%d").date(),
                                      time(23, 59, 59), pivot.tzinfo)
            pieces["UNTIL"] = cutoff.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            rule = ";".join(f"{k}={v}" for k, v in pieces.items())
        r = rrulestr(rule, dtstart=pivot)
        for dt in r.between(low, high, inc=True):
            when = dt.date() if all_day else dt
            dates[clock_key(when)] = when
    exdates = {clock_key(v): v for v in list_dates(record, "EXDATE")}
    return dates, exdates


def from_calendar(raw: bytes, source: str, left: date, right: date, *, publish=None, extra_cancel=None):
    publish = can_publish if publish is None else publish
    by_uid = defaultdict(list)
    for record in parse_ics(raw, source):
        uid = prop(record, "UID")
        if uid:
            by_uid[uid].append(record)
        else:
            raise BuildError(f"{source}: event is missing its UID")
    calc = []
    group = "local" if source == "upper" else "regional"
    url = UPPER_CAL if group == "local" else EMMAUS
    location = "" if group == "local" else "St. Paul Orthodox Church, Emmaus, PA"

    def add(record, parent, start, *, cancelled=False, duration=None):
        title = ics_text(prop(record, "SUMMARY", prop(parent, "SUMMARY")))
        desc = ics_text(prop(record, "DESCRIPTION", prop(parent, "DESCRIPTION")))
        if prop(record, "CLASS", prop(parent, "CLASS")).upper() in {"CONFIDENTIAL", "PRIVATE"}:
            return
        status = prop(record, "STATUS", prop(parent, "STATUS")).upper()
        if status == "TENTATIVE":
            return
        if prop(record, "RECURRENCE-ID") and record["RECURRENCE-ID"][-1][0].get("RANGE"):
            raise BuildError("range recurrence exception requires manual review")
        cancelled |= status == "CANCELLED" or bool(re.search(
            r"\bcancel(?:led|ed)\b|\bpostponed\b|^\s*no\b", title, flags=re.I))
        # Some calendars use free-text notes instead of status for a cancellation.
        cancelled |= bool(re.match(r"\s*(?:cancel(?:led|ed)|postponed)\b", desc, flags=re.I))
        if extra_cancel is not None:
            cancelled |= bool(extra_cancel(desc))
        if cancelled:
            title = cancellation_stem(title)
        if not publish(title, desc, source):
            return
        end = start + duration if duration is not None else None
        if not in_window(start, end, left, right):
            return
        title = re.sub(r"\bFamily Minstry Potluck\b", "Family Ministry Potluck", clean(title), flags=re.I)
        calc.append(Event(title, iso(start), iso(end) if end is not None else None,
                          source, url, group, location, cancelled=cancelled))

    def version(record):
        return int(prop(record, "SEQUENCE", "0")), prop(record, "DTSTAMP")

    for records in by_uid.values():
        bases = [r for r in records if not r.get("RECURRENCE-ID")]
        latest = max(bases, key=version) if bases else None
        # A master can be STATUS:CANCELLED without a DTSTART.
        base_start = dtprop(latest, "DTSTART")
        duration = span(latest, base_start) if base_start is not None else None
        base_cancelled = prop(latest, "STATUS").upper() == "CANCELLED"
        exceptions = {}
        for record in records:
            rid = dtprop(record, "RECURRENCE-ID")
            if rid is not None:
                if record["RECURRENCE-ID"][-1][0].get("RANGE"):
                    raise BuildError("range recurrence exception requires manual review")
                key = clock_key(rid)
                if key not in exceptions or version(record) >= version(exceptions[key][0]):
                    exceptions[key] = (record, rid)
        if base_start is not None:
            instances, exdates = recurrence(latest, base_start, left, right)
            for key, start in instances.items():
                if key not in exdates and key not in exceptions:
                    add(latest, None, start, duration=duration, cancelled=base_cancelled)
            for key, start in exdates.items():
                if key not in exceptions:
                    add(latest, None, start, duration=duration, cancelled=True)
        for record, old in exceptions.values():
            now = dtprop(record, "DTSTART", old)
            is_cancelled = base_cancelled or prop(record, "STATUS").upper() == "CANCELLED"
            if is_cancelled or clock_key(old) != clock_key(now):
                add(latest or record, None, old, duration=duration, cancelled=True)
            if is_cancelled:
                continue
            revised = span(record, now) if record.get("DTEND") or record.get("DURATION") else duration
            add(record, latest, now, duration=revised)
    return calc


class LetterLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and a.get("title", "").startswith("Download file:") and a.get("href"):
            u = urljoin(NEWS, a["href"])
            if safe_pdf(u) and u not in self.urls:
                self.urls.append(u)


MONTH_TOKEN = r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
MONTH_TOKEN += r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
DATED_LABEL = re.compile(rf"(?<!\w)({WEEKDAY_LIST}),?\s+({MONTH_LIST})\s+(\d{{1,2}})"
                         r"(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\s*:?(?!\d)", re.I)


def month_number(name: str) -> int:
    return next(i for m, i in MONTHS.items() if m.startswith(name.lower()))


def latest_letter(raw: bytes) -> str:
    p = LetterLinks()
    p.feed(raw.decode("utf-8-sig"))
    if not p.urls:
        raise BuildError("newsletter archive did not contain a recognized official PDF")
    # The archive is reverse chronological now. Also compare unambiguous dated filenames so
    # an editorial reorder of the known layouts does not pick a stale issue.
    dated = []
    for u in p.urls:
        name = u.rsplit("/", 1)[1].lower()
        nums = re.findall(r"(?<!\d)(20\d{2})(?!\d)", name)
        mm = re.search(rf"(?<![a-z])({MONTH_TOKEN})(?![a-z])", name)
        if len(nums) == 1 and mm:
            dated.append(((int(nums[0]), month_number(mm[1])), u))
        elif len(nums) == 1:
            mm = re.search(r"(?<!\d)(0[1-9]|1[0-2])_(20\d{2})(?!\d)", name)
            if mm:
                dated.append(((int(mm[2]), int(mm[1])), u))
    if not dated or p.urls[0] not in {u for _, u in dated}:
        raise BuildError("latest newsletter filename is ambiguous; verify the archive order")
    latest = max(version for version, _ in dated)
    current = [u for version, u in dated if version == latest]
    if len(current) != 1:
        raise BuildError("multiple PDF versions of the latest newsletter; choose the approved issue")
    return current[0]


def dated_label(match: re.Match, issue_year: int, issue_month: int) -> date:
    month = MONTHS[match[2].lower()]
    # At most one month adjacent when the year is not printed. Prefer omission
    # to assuming which year an unrelated callout mentions.
    year = int(match[4]) if match[4] else issue_year + (issue_month == 12 and month == 1)
    if abs((year - issue_year) * 12 + month - issue_month) > 1:
        raise BuildError("newsletter date is not adjacent to its issue")
    result = date(year, month, int(match[3]))
    if CLOCK[result.weekday()].lower() != match[1].lower():
        raise BuildError("weekday and newsletter date disagree")
    return result


def make_bulletin(title, when, url, *, note="", cancelled=False):
    return Event(title, iso(when), None, "bulletin", url, "local", "", note, cancelled)


CLOCK_CELL = re.compile(r"(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?", re.I)


def cells(line):
    return list(re.finditer(r"\S(?:.*?\S)?(?=\s{3,}|$)", line))


def nearby_clock(lines, offset, width=12):
    matches = []
    for line in lines:
        for c in cells(line):
            if abs(c.start() - offset) <= width and (m := CLOCK_CELL.fullmatch(c[0])):
                hour = int(m[1])
                if 1 <= hour <= 12:
                    matches.append(time(hour % 12 + (12 if m[3].lower() == "p" else 0), int(m[2] or 0)))
    return matches[0] if len(set(matches)) == 1 else None


def from_letter_text(raw: str, url: str, today: date):
    if not safe_pdf(url):
        raise BuildError("newsletter PDF is outside the parish source allowlist")
    issue = re.search(rf"\bNO\.\s*(20\d{{2}}):\s*(\d{{1,2}})\s*\|\s*({MONTH_LIST})\s+(20\d{{2}})",
                      raw[:2200], flags=re.I)
    if not issue or "THE DELIVERER" not in raw[:2200]:
        raise BuildError("newsletter masthead not recognized; use reviewed input for this issue")
    year, month = int(issue[1]), int(issue[2])
    if year != int(issue[4]) or month != MONTHS[issue[3].lower()]:
        raise BuildError("inconsistent newsletter masthead")
    age = (today.year - year) * 12 + today.month - month
    if age > 2 or age < -1:
        raise BuildError("newsletter is outside the expected publication window")
    events = []
    for page in raw.split("\f"):
        # Keep only explicit, matching named callouts. Never scan all prose for dates.
        if re.search(r"\bHopePHL\b", page, flags=re.I):
            hint = re.search(rf"\b(20\d{{2}})\s+HOLIDAY\s+DRIVE\b[\s\S]{{0,220}}?"
                             rf"\bbring\s+a\s+new\s+and\s+unwrapped\s+gift\s+to\s+"
                             rf"St\.\s+George\s+Church\s+by\s+({MONTH_LIST})\s+(\d{{1,2}})"
                             r"(?:st|nd|rd|th)?(?:[ ,]+(20\d{2}))?\b", page, flags=re.I)
            if hint and not NEGATIVE.search(hint[0]) and hint[1] == (hint[4] or hint[1]):
                m, y = MONTHS[hint[2].lower()], int(hint[1])
                if abs((y - year) * 12 + m - month) <= 1:
                    events.append(make_bulletin(
                        "HopePHL Holiday Gift Drive", date(y, m, int(hint[3])), url,
                        note="Gift drop-off deadline. Bring a new, unwrapped gift to St. George.",
                    ))
        # Lines in two-column PDFs are joined by spaces by Poppler. Match only the
        # exact printed cells, with the date on the next line in the same column.
        lines = page.splitlines()
        for i, line in enumerate(lines):
            for title, printed, date_on_next in (
                ("Nativity Pageant", "Nativity Pageant", True),
                ("St. Nicholas Day Special Coffee Hour", "St. Nicholas Day Special Coffee Hour", False),
            ):
                cell = next((c for c in cells(line) if c[0].casefold() == printed.casefold()), None)
                if cell is None or "save the date" not in page.casefold():
                    continue
                indices = [i + 1] if date_on_next else [i - 1, i - 2, i - 3]
                matched = []
                for idx in indices:
                    if idx < 0 or idx >= len(lines):
                        continue
                    for m in DATED_LABEL.finditer(lines[idx]):
                        if abs(m.start() - cell.start()) <= 12 and not NEGATIVE.search(lines[idx]):
                            matched.append(dated_label(m, year, month))
                if len(set(matched)) == 1:
                    note = ""
                    if date_on_next and i + 2 < len(lines) and re.search(
                        r"\bafter\s+(?:Divine\s+)?Liturgy\b", lines[i+2], flags=re.I
                    ):
                        note = "After Divine Liturgy."
                    events.append(make_bulletin(title, matched[0], url, note=note))
                    if title == "Nativity Pageant":
                        for j in range(i + 3, min(i + 6, len(lines) - 2)):
                            rehearsal = next((c for c in cells(lines[j])
                                              if c[0].casefold() == "dress rehearsal"
                                              and abs(c.start() - cell.start()) <= 12), None)
                            if rehearsal and (d := DATED_LABEL.search(lines[j+1])) and (
                                abs(d.start() - rehearsal.start()) <= 12 and not NEGATIVE.search(lines[j+1])
                            ) and (when := nearby_clock(lines[j+2:j+3], rehearsal.start())):
                                events.append(make_bulletin(
                                    "Nativity Pageant Dress Rehearsal",
                                    datetime.combine(dated_label(d, year, month), when, TZ), url,
                                ))
            # The observed November issue's multi-column calendar labels the
            # rightmost column Dec. 5; do not guess dates in other grids.
            if month == 11 and re.search(r"(?m)^.{120,}Dec\. 5\s*$", page):
                for c in cells(line):
                    if c[0] != "Pageant Rehearsal" or c.start() < 120:
                        continue
                    verified = any(any(other[0] == "Dec. 5" and abs(other.start() - c.start()) <= 6
                                       for other in cells(prev)) for prev in lines[max(0, i-4):i])
                    when = nearby_clock(lines[i+1:i+3], c.start())
                    if verified and when and not NEGATIVE.search(line):
                        events.append(make_bulletin(
                            "Nativity Pageant Rehearsal",
                            datetime.combine(date(year, 12, 5), when, TZ), url,
                        ))
    # November's printed grid labels Thanksgiving worship "tentative" while
    # the headline/calendar alone lack that qualifier. The news listing
    # deliberately leaves that particular conflicting service out.
    if month == 11 and re.search(r"(?m)Thanksgiving[^\n]*\n[^\n]*9:30 a\.m\."
                                 r"[^\n]*\n[^\n]*\(tentative\)", raw):
        nov = calendar.monthcalendar(year, 11)
        thursdays = [w[calendar.THURSDAY] for w in nov if w[calendar.THURSDAY]]
        events.append(make_bulletin("Liturgy for Thanksgiving", date(year, 11, thursdays[3]),
                                    url, cancelled=True))
    return list(set(events))


def from_newsletter(index: bytes, loader, today: date):
    url = latest_letter(index)
    pdf = loader(url)
    if len(pdf) > PDF_MAX or not pdf.startswith(b"%PDF-"):
        raise BuildError("newsletter response is not an allowed-sized PDF")
    try:
        p = subprocess.run(["pdftotext", "-layout", "-", "-"], input=pdf,
                           capture_output=True, check=True, timeout=25)
    except (FileNotFoundError, subprocess.SubprocessError) as exc:
        raise BuildError("cannot extract newsletter text; install Poppler or check this PDF") from exc
    if len(p.stdout) > TEXT_MAX:
        raise BuildError("newsletter text exceeds its size limit")
    events = from_letter_text(p.stdout.decode("utf-8-sig"), url, today)
    print(f"Newsletter: {url} ({len(events)} recognized callouts; other items require review).",
          file=sys.stderr)
    return events


class Plain(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
    def handle_starttag(self, tag, attrs):
        if tag in {"br", "p", "div"}:
            self.parts.append("\n")
    def handle_data(self, data):
        self.parts.append(data)


class ScheduleCard(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []

    def handle_starttag(self, tag, attrs):
        context = dict(attrs).get("data-current-context")
        if tag != "ul" or not context:
            return
        payload = json.loads(context)
        if not isinstance(payload, dict):
            return
        if "convention and conference schedule" not in payload.get("sectionTitle", "").casefold():
            return
        for item in payload.get("userItems", []):
            b = item.get("button") or {}
            t = normalize(item.get("title") or "")
            if b.get("buttonLink") in {"/new-york", DIOCESE_DETAIL} and all(
                phrase in t for phrase in ("new york", "washington", "charleston", "oakland", "mid atlantic")
            ):
                self.items.append(item)


CONFERENCE_DATE = re.compile(
    rf"^({MONTH_LIST})\s+(\d{{1,2}})\s*[–—-]\s*"
    rf"(?:({MONTH_LIST})\s+)?(\d{{1,2}}),?\s+(20\d{{2}})$", re.I
)
CONFERENCE_SINGLE = re.compile(rf"^({MONTH_LIST})\s+(\d{{1,2}}),?\s+(20\d{{2}})$", re.I)
CONFERENCE_VENUE = re.compile(r"St\. Paul Church\s*\(Emmaus, PA\)", re.I)


def from_diocese(raw: bytes):
    p = ScheduleCard()
    p.feed(raw.decode("utf-8-sig"))
    if len(p.items) != 1:
        raise BuildError("official diocesan master schedule changed; verify the Eastern row")
    desc = Plain()
    desc.feed(p.items[0].get("description", ""))
    lines = [" ".join(line.split()) for line in "".join(desc.parts).splitlines() if line.strip()]
    if len(lines) != 2 or not CONFERENCE_VENUE.fullmatch(lines[0]) or NEGATIVE.search(lines[1]):
        raise BuildError("diocesan venue/date format changed; verify the Eastern row")
    if m := CONFERENCE_DATE.fullmatch(lines[1]):
        year = int(m[5])
        mo_start, mo_end = MONTHS[m[1].lower()], MONTHS[(m[3] or m[1]).lower()]
        if mo_end < mo_start:
            raise BuildError("diocesan date range crosses years ambiguously")
        start = date(year, mo_start, int(m[2]))
        inclusive_end = date(year, mo_end, int(m[4]))
    elif m := CONFERENCE_SINGLE.fullmatch(lines[1]):
        start = date(int(m[3]), MONTHS[m[1].lower()], int(m[2]))
        inclusive_end = start
    else:
        raise BuildError("diocesan date format changed; verify the Eastern row")
    if not timedelta(0) <= inclusive_end - start <= timedelta(days=10):
        raise BuildError("diocesan schedule has an implausible conference duration")
    # The underlying program may include services, but the conference master schedule
    # independently lists the conference itself, not any of its worship sessions.
    return [Event("Eastern Dioceses Parish Life Conference", start.isoformat(),
                  (inclusive_end + DELTA).isoformat(), "diocese", DIOCESE,
                  "regional", "Hosted by St. Paul Church (Emmaus, PA)")]


def read_reviewed(path: Path):
    data = json.loads(read_bounded(path).decode("utf-8-sig"))
    if not isinstance(data, dict) or set(data) != {"schema_version", "events"} or (
        data["schema_version"] != 1 or not isinstance(data["events"], list) or len(data["events"]) > 100
    ):
        raise BuildError("reviewed input must be a version 1 object with up to 100 events")
    result = []
    required = {"title", "start", "source_url", "group"}
    optional = {"end", "location", "note", "cancelled"}
    for entry in data["events"]:
        if not isinstance(entry, dict) or not required <= set(entry) <= required | optional or not all(
            isinstance(v, str) for k, v in entry.items() if k != "cancelled"
        ) or not isinstance(entry.get("cancelled", False), bool):
            raise BuildError("reviewed event has missing or invalid fields")
        group = entry["group"]
        if group not in {"local", "regional"} or not safe_review_url(entry["source_url"], group):
            raise BuildError("reviewed event source must be an approved official HTTPS page")
        title = clean(entry["title"])
        note = clean(entry.get("note", ""), 240)
        location = clean(entry.get("location", ""), 140)
        if not title or NEGATIVE.search(title) or INTERNAL.search(title):
            raise BuildError("reviewed title is tentative, private or invalid; use cancelled: true")
        if group == "regional" and WORSHIP.search(" ".join((title, note))):
            raise BuildError("regional input contains worship or a combined program")
        start = parse_iso(entry["start"])
        end = checked_duration(start, parse_iso(entry["end"])) if "end" in entry else None
        result.append(Event(title, iso(start), iso(end) if end is not None else None,
                            "reviewed_" + group, entry["source_url"], group, location,
                            note, entry.get("cancelled", False)))
    return result


def reconcile(records, left: date, local_days: int, regional_days: int):
    totals = defaultdict(list)
    for e in records:
        start = parse_iso(e.start)
        end = parse_iso(e.end) if e.end else None
        horizon = local_days if e.group == "local" else regional_days
        if in_window(start, end, left, left + timedelta(days=horizon)):
            totals[(*event_key(e), day(start))].append(e)
    winners = []
    for rows in totals.values():
        # A matching cancellation defeats every older/less reliable source. A
        # same-day, independently confirmed different time is not overwritten.
        active = [e for e in rows if not e.cancelled and not any(
            c.cancelled and (RANK[c.source] <= RANK[e.source] or (
                c.source == "bulletin" and e.source == "upper"
                and normalize(c.title) == "liturgy for thanksgiving"
            ))
            and (
                c.start == e.start or "T" not in e.start or "T" not in c.start
                or (c.source == "upper" and e.source == "bulletin"
                    and "pageant" in normalize(e.title))
            )
            for c in rows
        )]
        # For explicitly recognized, same-day local pageant schedule conflicts,
        # show the newsletter's printed time, not a second conflicting card.
        # Keep any authorized reviewed update and honor calendar cancellations.
        bulletin_times = {e.start for e in active if e.source == "bulletin"
                          and "T" in e.start and "pageant" in normalize(e.title)}
        if len(bulletin_times) == 1:
            active = [e for e in active if not (
                e.source == "upper" and "T" in e.start and e.start not in bulletin_times
                and "pageant" in normalize(e.title)
            )]
        exact = defaultdict(list)
        for e in active:
            exact[e.start].append(e)
        precise = [k for k in exact if "T" in k]
        if len(precise) == 1:
            # Attach exact-date cross-posts to the one confirmed time, as long as
            # they really are the same event and do not span multiple dates.
            for k in list(exact):
                if "T" not in k:
                    for e in exact[k]:
                        if end_day(e) == day(parse_iso(e.start)):
                            exact[precise[0]].append(e)
                    exact[k] = [e for e in exact[k] if end_day(e) != day(parse_iso(e.start))]
        for stamp, subset in exact.items():
            if not subset:
                continue
            subset.sort(key=lambda e: (RANK[e.source], e.start != stamp, e.source_url, e.title, e.end or ""))
            display = subset[0]
            timing = next(e for e in subset if e.start == stamp)
            best_note = next((e.note for e in subset if e.note), "")
            # "After Divine Liturgy" is a printed event timing qualifier. Do not
            # render a more precise-looking planned time on the public card.
            if best_note == "After Divine Liturgy." and display.group == "local":
                display = next(e for e in subset if e.note == best_note)
                display = replace(display, start=day(parse_iso(stamp)).isoformat(), end=None)
            else:
                display = replace(display, start=timing.start, end=timing.end)
            winners.append(replace(display, note=best_note))
    return sorted(winners, key=lambda e: (e.start, e.group, normalize(e.title), e.source_url))


def read_bounded(path: Path, limit: int = TEXT_MAX) -> bytes:
    with path.open("rb") as f:
        data = f.read(limit + 1)
    if not data or len(data) > limit:
        raise BuildError(f"{path.name}: empty or too large")
    return data


def clean_out(e: Event) -> dict:
    out = asdict(e)
    del out["cancelled"]
    out["id"] = hashlib.sha256(f"{event_key(e)}|{e.start}".encode()).hexdigest()[:20]
    out["source_label"] = PUBLIC_SOURCE[e.source]
    return out


def out_json(events: list[Event], today: date) -> str:
    return json.dumps({"schema_version": 1, "as_of": today.isoformat(),
                       "timezone": TZ.key, "events": [clean_out(e) for e in events]},
                      indent=2, ensure_ascii=True).replace("<", "\\u003c") + "\n"


def format_day(d: date) -> str:
    return f"{CLOCK[d.weekday()]}, {calendar.month_name[d.month]} {d.day}, {d.year}"


def format_clock(t: datetime) -> str:
    return f"{t.hour % 12 or 12}:{t.minute:02d} {'a.m.' if t.hour < 12 else 'p.m.'}"


def format_interval(e: Event) -> str:
    start = parse_iso(e.start)
    end = parse_iso(e.end) if e.end is not None else None
    display = format_day(day(start))
    if isinstance(start, datetime):
        display += f" at {format_clock(start)}"
        if isinstance(end, datetime) and end != start:
            display += " to "
            if day(end) != day(start):
                display += format_day(day(end)) + " at "
            display += format_clock(end)
        return display + " ET"
    if end_day(e) > day(start):
        display += f" to {format_day(end_day(e))}"
    return display


def out_html(events: list[Event], today: date) -> str:
    esc = html.escape
    elements = ['<section class="stg-news-events" aria-labelledby="stg-news-heading">',
                '<h2 id="stg-news-heading">Upcoming News &amp; Events</h2>',
                '<p>Dates and times are Eastern. Check each source for changes.</p>']
    for group, heading in (("local", "At St. George"), ("regional", "Around the region")):
        elements.append(f"<h3>{heading}</h3>")
        members = [e for e in events if e.group == group]
        if not members:
            elements.append("<p>No featured events in this listing. Check the calendars below.</p>")
            continue
        elements.append('<ul class="stg-event-list">')
        for e in members:
            elements.extend(("<li>", f"<strong>{esc(e.title)}</strong><br>",
                             f'<time datetime="{esc(e.start, quote=True)}">{esc(format_interval(e))}</time>'))
            for t in (e.location, e.note):
                if t:
                    elements.append(f"<br>{esc(t)}")
            elements.append(f'<br><a href="{esc(e.source_url, quote=True)}">'
                            f'{esc(PUBLIC_SOURCE[e.source])}</a></li>')
        elements.append("</ul>")
    elements.append(f'<p><a href="{UPPER_CAL}">Full St. George calendar</a> · '
                    f'<a href="{NEWS}">Newsletter archive</a> · '
                    f'<a href="{DIOCESE}">Diocesan conference schedule</a></p>')
    elements.append(f'<p><small>Listing checked {esc(format_day(today))}.</small></p>')
    elements.append("</section>")
    return "\n".join(elements) + "\n"


def atomic_write(path: Path, output: str):
    # The temporary file is created in the same directory so os.replace is atomic.
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n",
                                         dir=path.parent, prefix=".stg-events-", delete=False) as f:
            name = f.name
            f.write(output)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def run(args) -> list[Event]:
    today = date.fromisoformat(args.as_of) if args.as_of else datetime.now(TZ).date()
    if not 1 <= args.local_days <= 366 or not 1 <= args.regional_days <= 366:
        raise BuildError("horizons must be 1 to 366 days")
    fixture_dir = args.input_dir

    def pull(url, filename, limit=TEXT_MAX):
        return read_bounded(fixture_dir / filename, limit) if fixture_dir else fetch(url, limit=limit)

    events = from_calendar(pull(UPPER_ICS, "upper.ics"), "upper", today,
                           today + timedelta(days=args.local_days))
    if not args.no_emmaus:
        events += from_calendar(pull(EMMAUS_ICS, "emmaus.ics"), "emmaus", today,
                                today + timedelta(days=args.regional_days))
    if not args.no_newsletter:
        events += from_newsletter(pull(NEWS, "newsletters.html"),
                                  lambda url: pull(url, "newsletter.pdf", PDF_MAX), today)
    if not args.no_diocese:
        events += from_diocese(pull(DIOCESE, "diocese.html"))
    if args.reviewed:
        events += read_reviewed(args.reviewed)
    events = reconcile(events, today, args.local_days, args.regional_days)
    output = out_json(events, today) if args.format == "json" else out_html(events, today)
    atomic_write(args.output, output)
    return events


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True, help="local path to atomically replace after success")
    p.add_argument("--format", default="html", choices=("html", "json"))
    p.add_argument("--local-days", type=int, default=90)
    p.add_argument("--regional-days", type=int, default=270)
    p.add_argument("--as-of", help="override America/New_York date (YYYY-MM-DD) for testing")
    p.add_argument("--input-dir", type=Path, help="read approved-source test snapshots; no network")
    p.add_argument("--reviewed", type=Path, help="optional reviewed, public version-1 JSON")
    p.add_argument("--no-emmaus", action="store_true", help="disable the Emmaus source")
    p.add_argument("--no-newsletter", action="store_true", help="disable the newsletter source")
    p.add_argument("--no-diocese", action="store_true", help="disable the diocesan source")
    args = p.parse_args()
    try:
        events = run(args)
    except (BuildError, OSError, ValueError, TypeError, OverflowError, AttributeError,
            KeyError, ZoneInfoNotFoundError) as exc:
        print(f"Build failed; previous output was not changed. {exc}", file=sys.stderr)
        return 1
    print(f"Generated {len(events)} events in {args.output}. No website changes made.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
