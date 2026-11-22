"""Offline, synthetic fixtures; actual calendar data is not committed here."""
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from build_events import BuildError, UPPER_ICS
from build_next_service import main, render, upcoming


def calendar(*rows, name="St. George Orthodox Church"):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"X-WR-CALNAME:{name}"]
    for row in rows:
        lines += ["BEGIN:VEVENT", *row, "END:VEVENT"]
    return ("\r\n".join(lines + ["END:VCALENDAR", ""])).encode()


def local(value):
    return f"DTSTART;TZID=America/New_York:{value}"


def recurrence_id(value):
    return f"RECURRENCE-ID;TZID=America/New_York:{value}"


def as_of(value):
    return datetime.fromisoformat(value)


AFTER_THANKSGIVING = as_of("2026-11-26T12:00:00-05:00")
VESPER_DUPLICATES = calendar(
    ["UID:older-weekly", local("20261121T170000"), "SUMMARY:Vespers ",
     "RRULE:FREQ=WEEKLY;COUNT=8"],
    ["UID:expect-cancellation", local("20261121T170000"), "SUMMARY:Great Vespers",
     "RRULE:FREQ=WEEKLY;COUNT=2"],
    ["UID:expect-cancellation", recurrence_id("20261128T170000"), local("20261128T170000"),
     "SUMMARY:CANCELLED Vespers", "STATUS:CONFIRMED"],
    ["UID:modern-weekly", local("20261205T163000"), "SUMMARY:Great Vespers",
     "RRULE:FREQ=WEEKLY;COUNT=6"],
    ["UID:modern-weekly", recurrence_id("20261212T163000"), local("20261212T163000"),
     "SUMMARY:Family Vespers", "STATUS:CONFIRMED", "SEQUENCE:1"],
    ["UID:modern-weekly", recurrence_id("20261226T163000"), local("20261226T163000"),
     "SUMMARY:NO Vespers", "STATUS:CONFIRMED"],
    ["UID:matins", local("20261129T090000"), "SUMMARY:Matins", "RRULE:FREQ=WEEKLY;COUNT=8"],
    ["UID:liturgy", local("20261129T100000"), "SUMMARY:Divine Liturgy", "RRULE:FREQ=WEEKLY;COUNT=8"],
)


class NextServiceTests(unittest.TestCase):
    def test_cancels_november_28_duplicate_and_uses_sunday_morning(self):
        rows = upcoming(VESPER_DUPLICATES, AFTER_THANKSGIVING)
        self.assertEqual([(title, start.isoformat()) for title, start in rows[:3]], [
            ("Matins", "2026-11-29T09:00:00-05:00"),
            ("Divine Liturgy", "2026-11-29T10:00:00-05:00"),
            ("Great Vespers", "2026-12-05T16:30:00-05:00"),
        ])
        self.assertNotIn("2026-11-28", render(rows, AFTER_THANKSGIVING))

    def test_older_vespers_series_does_not_reappear_after_replacement(self):
        rows = upcoming(VESPER_DUPLICATES, as_of("2026-12-05T16:45:00-05:00"))
        self.assertEqual(rows[0][0], "Matins")
        self.assertNotIn("2026-12-05T17:00:00-05:00", [x[1].isoformat() for x in rows])
        self.assertIn(("Family Vespers", "2026-12-12T16:30:00-05:00"),
                      [(title, start.isoformat()) for title, start in rows])

    def test_no_vespers_cancels_new_and_old_times_on_same_day(self):
        rows = upcoming(VESPER_DUPLICATES, as_of("2026-12-26T12:00:00-05:00"))
        self.assertEqual(rows[0][1].date().isoformat(), "2026-12-27")

    def test_timezone_and_same_day_passed_time_across_dst(self):
        raw = calendar(
            ["UID:season", local("20261025T090000"), "SUMMARY:Matins", "RRULE:FREQ=WEEKLY;COUNT=3"],
            ["UID:utc", "DTSTART:20261101T150000Z", "SUMMARY:Divine Liturgy"],
        )
        rows = upcoming(raw, as_of("2026-10-25T12:00:00Z"))
        self.assertEqual([(x.isoformat()) for _, x in rows], ["2026-10-25T09:00:00-04:00",
                         "2026-11-01T09:00:00-05:00", "2026-11-01T10:00:00-05:00"])
        output = render(rows, as_of("2026-10-25T12:00:00Z"))
        self.assertIn("9:00 a.m. EDT", output)
        self.assertIn("9:00 a.m. EST", output)
        rows = upcoming(raw, as_of("2026-11-01T14:00:00Z"))
        self.assertEqual(rows[0][0], "Divine Liturgy")

    def test_recurring_omission_status_cancel_move_and_free_text(self):
        raw = calendar(
            ["UID:weekly", local("20261122T090000"), "SUMMARY:Matins",
             "RRULE:FREQ=WEEKLY;COUNT=4", "EXDATE;TZID=America/New_York:20261122T090000"],
            ["UID:weekly", recurrence_id("20261129T090000"), local("20261129T093000"),
             "SUMMARY:Matins", "SEQUENCE:1"],
            ["UID:weekly", recurrence_id("20261206T090000"), "STATUS:CANCELLED", "SEQUENCE:2"],
            ["UID:dup-moved", local("20261129T090000"), "SUMMARY:Orthros"],
            ["UID:dup-canceled", local("20261206T090000"), "SUMMARY:Orthros"],
            ["UID:note-1", local("20261206T100000"), "SUMMARY:Divine Liturgy"],
            ["UID:note-2", local("20261206T100000"), "SUMMARY:Liturgy",
             "DESCRIPTION:Update: This liturgy has been postponed."],
        )
        self.assertEqual([(title, start.isoformat()) for title, start in upcoming(raw, AFTER_THANKSGIVING)],
                         [("Matins", "2026-11-29T09:30:00-05:00")])

    def test_private_online_tentative_all_day_and_unsafe_fields_not_rendered(self):
        raw = calendar(
            ["UID:secret-uid", local("20261126T180000"), "SUMMARY:Matins", "CLASS:PRIVATE"],
            ["UID:private-b", local("20261126T190000"), "SUMMARY:Matins",
             "DESCRIPTION:<strong>Private</strong> service under cover"],
            ["UID:private-series", local("20261127T090000"), "SUMMARY:Divine Liturgy",
             "RRULE:FREQ=DAILY;COUNT=3", "CLASS:CONFIDENTIAL"],
            ["UID:private-series", recurrence_id("20261128T090000"), local("20261128T090000"),
             "SUMMARY:Divine Liturgy", "CLASS:PUBLIC"],
            ["UID:offsite", local("20261127T180000"), "SUMMARY:Vespers", "LOCATION:St. Other"],
            ["UID:online", local("20261127T190000"), "SUMMARY:Akathist (online)"],
            ["UID:zoom", local("20261127T193000"), "SUMMARY:Akathist",
             "DESCRIPTION:Service will be virtual"],
            ["UID:notconfirmed", local("20261127T200000"), "SUMMARY:Vespers", "STATUS:TENTATIVE"],
            ["UID:choir", local("20261128T080000"), "SUMMARY:Youth Choir Singing Liturgy"],
            ["UID:named-wedding", local("20261128T080000"), "SUMMARY:Vespers for Jane Doe"],
            ["UID:all-day", "DTSTART;VALUE=DATE:20261128", "SUMMARY:Divine Liturgy"],
            ["UID:pub", local("20261129T090000"), "SUMMARY:Matins",
             "LOCATION:8210 W. Chester Pike", "DESCRIPTION:Jane Doe 555-000-5555"],
        )
        rows = upcoming(raw, AFTER_THANKSGIVING)
        self.assertEqual([x[0] for x in rows], ["Matins"])
        page = render(rows, AFTER_THANKSGIVING)
        for private in ("secret-uid", "under cover", "St. Other", "online", "virtual", "8210 W.",
                        "Jane Doe", "555-000-5555", "<strong>Private</strong>"):
            self.assertNotIn(private, page)
        with self.assertRaises(BuildError):
            upcoming(raw, as_of("2026-11-26T12:00:00"))
        with self.assertRaises(BuildError):
            upcoming(calendar(["UID:pub", local("20261129T090000"), "SUMMARY:Matins"],
                              name="St. Paul Orthodox Church"), AFTER_THANKSGIVING)

    def test_only_public_calendar_fetched_and_build_failure_preserves_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "next-service.html"
            args = ["build_next_service.py", "--output", str(path),
                    "--as-of", AFTER_THANKSGIVING.isoformat()]
            with patch("sys.argv", args), patch("build_next_service.fetch", return_value=VESPER_DUPLICATES) as get:
                self.assertEqual(main(), 0)
                get.assert_called_once_with(UPPER_ICS)
            good = path.read_bytes()
            with patch("sys.argv", args), patch("build_next_service.fetch", return_value=b"<not-ics>"):
                self.assertEqual(main(), 1)
            self.assertEqual(path.read_bytes(), good)

    @unittest.skipUnless(shutil.which("node"), "Node is required for the browser-clock test")
    def test_client_clock_switches_matins_to_liturgy_and_hides_stale_page(self):
        now = as_of("2026-11-29T08:59:30-05:00")
        script = re.search(r"<script>(.*?)</script>", render(upcoming(VESPER_DUPLICATES, now), now), re.S)[1]
        harness = r"""
const assert = require('node:assert/strict');
let fake = Date.parse('2026-11-29T08:59:30-05:00');
let notified;
const rows = ['2026-11-29T09:00:00-05:00', '2026-11-29T10:00:00-05:00',
  '2026-12-05T16:30:00-05:00'].map(time => ({hidden:true,dataset:{start:Date.parse(time)}}));
const status = {hidden:false};
const ready = {Date:{now:()=>fake}, setTimeout:(callback)=>{notified=callback;return 1;},
  clearTimeout:()=>{}, document:{querySelectorAll:()=>rows, getElementById:()=>status,
  addEventListener:()=>{}}, window:{addEventListener:()=>{}}};
const context = require('node:vm').createContext(ready);
require('node:vm').runInContext(require('node:fs').readFileSync(0,'utf8'), context);
assert.deepEqual(rows.map(x=>x.hidden), [false,true,true]); assert.equal(status.hidden,true);
fake += 30000; notified();
assert.deepEqual(rows.map(x=>x.hidden), [true,false,true]); assert.equal(status.hidden,true);
fake += 31*60*60*1000; notified();
assert.deepEqual(rows.map(x=>x.hidden), [true,true,true]); assert.equal(status.hidden,false);
"""
        subprocess.run(["node", "-e", harness], input=script, text=True, check=True)


if __name__ == "__main__":
    unittest.main()
