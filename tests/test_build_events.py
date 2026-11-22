import html
import json
import unittest
from datetime import date

import build_events as app

TODAY = date(2026, 11, 22)


def feed(*records, source='upper'):
    fields = ['BEGIN:VCALENDAR', 'VERSION:2.0', 'X-WR-CALNAME:' + app.KNOWN_SOURCE_NAMES[source]]
    for record in records:
        fields += ['BEGIN:VEVENT', *record, 'END:VEVENT']
    return ('\r\n'.join(fields + ['END:VCALENDAR', ''])).encode()


class PublicFeedTests(unittest.TestCase):
    def test_other_parish_worship_is_excluded(self):
        def event(uid, title, *rest):
            return ('UID:' + uid, 'DTSTART;VALUE=DATE:20261201', 'SUMMARY:' + title, *rest)
        fixture = feed(event('a', 'Thanksgiving Akathist'), event('b', 'Vespers & Retreat'),
                       event('c', 'Family Night', 'CLASS:PRIVATE'),
                       event('d', 'Community Festival'), source='emmaus')
        out = app.from_calendar(fixture, 'emmaus', TODAY, date(2026, 12, 10))
        self.assertEqual([e.title for e in out], ['Community Festival'])

    def test_timed_google_date_until_is_inclusive(self):
        fixture = feed(('UID:x', 'DTSTART;TZID=America/New_York:20261205T230000',
                        'RRULE:FREQ=DAILY;UNTIL=20261206', 'SUMMARY:Community Festival'))
        out = app.from_calendar(fixture, 'upper', TODAY, date(2026, 12, 10))
        self.assertEqual([e.start for e in out], ['2026-12-05T23:00:00-05:00', '2026-12-06T23:00:00-05:00'])

    def test_official_diocesan_conference_is_bounded_to_its_dates(self):
        data = {'sectionTitle': '2027 Convention and Conference Schedule', 'userItems': [{
            'title': 'New York, Washington, D.C.; Charleston and Oakland & Mid-Atlantic',
            'description': '<p>St. Paul Church (Emmaus, PA)<br>June 30-July 3, 2027</p>',
            'button': {'buttonLink': '/new-york'}}]}
        fixture = ('<ul data-current-context="' + html.escape(json.dumps(data), quote=True) + '"></ul>').encode()
        out = app.from_diocese(fixture)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].start, '2027-06-30')
        self.assertEqual(out[0].end, '2027-07-04')
