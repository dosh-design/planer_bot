"""Calendar import from iPhone / Android: .ics text and links."""
import datetime as dt

import pytest

import db
import ics
from test_web import client, init_data  # noqa: F401  (client is a fixture)

H1 = {"X-Init-Data": init_data(1, "Маша")}
TODAY = dt.date(2026, 10, 28)

CAL = """BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//Apple Inc.//iPhone OS 18//EN\r
BEGIN:VEVENT\r
UID:meet-1\r
SUMMARY:Стоматолог\\, контроль\r
DTSTART;TZID=Europe/Moscow:20261103T183000\r
DTEND;TZID=Europe/Moscow:20261103T193000\r
BEGIN:VALARM\r
TRIGGER:-PT15M\r
DESCRIPTION:не заголовок\r
END:VALARM\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:utc-1\r
SUMMARY:Созвон с очень длинным\r
  названием\r
DTSTART:20261105T070000Z\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:bday-1\r
SUMMARY:День рождения Вари\r
DTSTART;VALUE=DATE:20150304\r
RRULE:FREQ=YEARLY\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:old-1\r
SUMMARY:Прошлое\r
DTSTART:20250101T100000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:gym\r
SUMMARY:Бассейн\r
DTSTART:20250901T080000\r
DTEND:20250901T090000\r
RRULE:FREQ=WEEKLY;BYDAY=MO\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:cancel\r
SUMMARY:Отменили\r
STATUS:CANCELLED\r
DTSTART:20261110T100000\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:trip\r
SUMMARY:Отпуск\r
DTSTART;VALUE=DATE:20261201\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_parse_takes_what_the_planner_can_show():
    got = {e.uid: e for e in ics.parse(CAL, TODAY, "Europe/Moscow")}
    assert set(got) == {"meet-1", "utc-1", "bday-1", "gym", "trip"}  # past and cancelled are skipped
    m = got["meet-1"]
    assert (m.text, m.day, m.time, m.end) == ("Стоматолог, контроль", "2026-11-03", "18:30", "19:30")
    assert (got["utc-1"].text, got["utc-1"].time) == ("Созвон с очень длинным названием", "10:00")  # UTC → Moscow
    assert (got["bday-1"].day, got["bday-1"].repeat) == ("2027-03-04", "year")
    assert (got["gym"].day, got["gym"].weekday, got["gym"].time) == (None, 0, "08:00")
    assert got["trip"].all_day and got["trip"].time is None


def test_monthly_and_daily_give_the_next_time():
    cal = ("BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:rent\nSUMMARY:Аренда\nDTSTART;VALUE=DATE:20260105\nRRULE:FREQ=MONTHLY\n"
           "END:VEVENT\nBEGIN:VEVENT\nUID:over\nSUMMARY:Курс\nDTSTART:20260101T090000\nRRULE:FREQ=DAILY;UNTIL=20260301T000000Z\n"
           "END:VEVENT\nEND:VCALENDAR\n")
    got = ics.parse(cal, TODAY, "Europe/Moscow")
    assert [(e.uid, e.day) for e in got] == [("rent", "2026-11-05")]  # a series that ended is skipped


def test_garbage_does_not_crash():
    assert ics.parse("BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:сломано\nEND:VEVENT\nEND:VCALENDAR", TODAY, "Europe/Moscow") == []


@pytest.mark.parametrize("url", ["http://example.com/a.ics", "https://127.0.0.1/a.ics", "https://localhost/x",
                                 "file:///etc/passwd", "https://user@example.com/a.ics", "https://10.0.0.1:8443/a"])
def test_unsafe_links_are_refused(url):
    assert ics.link_ok(url) is None


async def test_import_file_adds_once_and_partner_sees_it(client, couple, monkeypatch):  # noqa: F811
    monkeypatch.setattr(db, "local_now", lambda: dt.datetime(2026, 10, 28, 9, 0))
    r = await client.post("/api/import", json={"ics": CAL}, headers=H1)
    assert (await r.json()) == {"found": 5, "added": 5}
    again = await (await client.post("/api/import", json={"ics": CAL}, headers=H1)).json()
    assert again == {"found": 5, "added": 0}
    h2 = {"X-Init-Data": init_data(2, "Дима")}
    st = await (await client.get("/api/state", headers=h2)).json()
    by = {i["text"]: i for i in st["items"]}
    assert by["Стоматолог, контроль"]["kind"] == "busy" and by["Стоматолог, контроль"]["assignee"] == 1
    assert by["День рождения Вари"]["kind"] == "event" and by["День рождения Вари"]["assignee"] is None


async def test_import_rejects_non_calendars(client, couple):  # noqa: F811
    for body in ({"ics": "просто текст"}, {"ics": 5}, {}, {"url": "http://example.com"}, {"url": "https://127.0.0.1/x"}):
        r = await client.post("/api/import", json=body, headers=H1)
        assert r.status == 400
