import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from build_events import BuildError, UPPER_ICS
from build_parish_calendar import main, render, upcoming


def calendar(*rows, name="St. George Orthodox Church"):
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", f"X-WR-CALNAME:{name}"]
    for row in rows:
        lines += ["BEGIN:VEVENT", *row, "END:VEVENT"]
    return ("\r\n".join(lines + ["END:VCALENDAR", ""])).encode()


TODAY = date(2026, 10, 30)


class ParishCalendarTests(unittest.TestCase):
    def test_scoped_titles_and_no_raw_metadata(self):
        raw = calendar(
            ["UID:special", "DTSTART:20261101T143000Z", "SUMMARY:Hierarchical Divine Liturgy",
             "LOCATION:Sample room", "DESCRIPTION:Sample notes"],
            ["UID:potluck", "DTSTART;VALUE=DATE:20261107", "SUMMARY:Parish Picnic & Potluck"],
            ["UID:ordinary", "DTSTART:20261108T143000Z", "SUMMARY:Divine Liturgy",
             "RRULE:FREQ=WEEKLY;COUNT=4"],
            ["UID:annual", "DTSTART;VALUE=DATE:20261126", "SUMMARY:Liturgy for Thanksgiving"],
            ["UID:feast", "DTSTART;VALUE=DATE:20261108", "SUMMARY:St. Nicholas Day"],
            ["UID:roster", "DTSTART;VALUE=DATE:20261108", "SUMMARY:Youth Choir Singing Liturgy"],
            ["UID:rehearsal", "DTSTART;VALUE=DATE:20261108", "SUMMARY:Nativity Pageant Rehearsal"],
            ["UID:private", "DTSTART;VALUE=DATE:20261110", "SUMMARY:Parish Picnic", "CLASS:PRIVATE"],
            ["UID:private2", "DTSTART;VALUE=DATE:20261111", "SUMMARY:Special Vespers", "DESCRIPTION:Private service"],
            ["UID:tentative", "DTSTART;VALUE=DATE:20261112", "SUMMARY:Concert", "STATUS:TENTATIVE"],
        )
        events = upcoming(raw, TODAY)
        self.assertEqual([e.title for e in events], ["Hierarchical Divine Liturgy", "Parish Picnic & Potluck"])
        self.assertEqual(events[0].start, "2026-11-01T09:30:00-05:00")
        page = render(events, TODAY)
        self.assertIn("Parish Picnic &amp; Potluck", page)
        self.assertIn("9:30 a.m. ET", page)
        for unexpected in ("Sample room", "Sample notes", "newsletter", "Emmaus"):
            self.assertNotIn(unexpected, page)

    def test_exdate_cancellation_and_moved_occurrence(self):
        raw = calendar(
            ["UID:recurring", "DTSTART;TZID=America/New_York:20261101T170000", "SUMMARY:Family Vespers",
             "RRULE:FREQ=WEEKLY;COUNT=4", "EXDATE;TZID=America/New_York:20261108T170000"],
            ["UID:recurring", "RECURRENCE-ID;TZID=America/New_York:20261115T170000",
             "DTSTART;TZID=America/New_York:20261114T160000", "SEQUENCE:1", "SUMMARY:Family Vespers"],
            ["UID:recurring", "RECURRENCE-ID;TZID=America/New_York:20261122T170000", "STATUS:CANCELLED", "SEQUENCE:2"],
            ["UID:cancel-copy", "DTSTART;TZID=America/New_York:20261101T170000", "SUMMARY:Family Vespers",
             "DESCRIPTION:Update: This service is cancelled."],
        )
        self.assertEqual([e.start for e in upcoming(raw, TODAY)], ["2026-11-14T16:00:00-05:00"])

    def test_only_the_primary_ics_is_fetched_and_bad_source_keeps_previous_page(self):
        raw = calendar(["UID:x", "DTSTART;VALUE=DATE:20261107", "SUMMARY:Parish Picnic"])
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "listing.html"
            args = ["build_parish_calendar.py", "--output", str(out), "--as-of", TODAY.isoformat()]
            with patch("sys.argv", args), patch("build_parish_calendar.fetch", return_value=raw) as get:
                self.assertEqual(main(), 0)
                get.assert_called_once_with(UPPER_ICS)
            previous = out.read_bytes()
            with patch("sys.argv", args), patch("build_parish_calendar.fetch", return_value=b"<html>bad</html>"):
                self.assertEqual(main(), 1)
            self.assertEqual(out.read_bytes(), previous)
        with self.assertRaises(BuildError):
            upcoming(calendar(["UID:x", "DTSTART;VALUE=DATE:20261107", "SUMMARY:Parish Picnic"],
                              name="Another Calendar"), TODAY)


if __name__ == "__main__":
    unittest.main()
