import datetime as dt
import json

import parser

THU = dt.date(2026, 10, 22)  # a Thursday


def one(text, today=THU):
    items = parser.rules(text, today)
    assert len(items) == 1, items
    return items[0]


def test_task_for_partner_with_weekday():
    it = one("Ты обещал вкрутить лампочку в ванной до субботы")
    assert it.kind == "task" and it.who == "partner"
    assert it.date == "2026-10-24"
    assert "лампочку" in it.text and "суббот" not in it.text.lower()


def test_same_weekday_means_next_week():
    assert one("позвонить сантехнику в четверг").date == "2026-10-29"


def test_relative_dates_and_time():
    assert one("завтра в 19:00 забрать посылку").date == "2026-10-23"
    it = one("послезавтра в 7 вечера вынести мусор")
    assert it.date == "2026-10-24" and it.time == "19:00"
    assert one("через 3 дня оплатить интернет").date == "2026-10-25"
    assert one("через неделю поменять фильтр").date == "2026-10-29"


def test_explicit_dates():
    assert one("забрать документы 30.10").date == "2026-10-30"
    assert one("продлить страховку 5.01").date == "2027-01-05"  # past day-month rolls to next year
    assert one("годовщина 12 ноября").date == "2026-11-12"


def test_shopping_list_splits():
    items = parser.rules("Купить хлеб, молоко и батарейки", THU)
    assert [i.kind for i in items] == ["buy"] * 3
    assert [i.text for i in items] == ["Хлеб", "Молоко", "Батарейки"]


def test_weekly_busy():
    it = one("По четвергам у меня футбол")
    assert it.kind == "busy" and it.weekly == 3 and it.date is None
    it = one("в субботу баня с друзьями")
    assert it.kind == "busy" and it.date == "2026-10-24"


def test_joint_event():
    it = one("В воскресенье едем к маме")
    assert it.kind == "event" and it.who == "both" and it.date == "2026-10-25"


def test_model_answer_is_cleaned():
    raw = "Вот: " + json.dumps({"items": [
        {"kind": "task", "text": "вкрутить лампочку", "who": "partner", "date": "2026-10-24", "time": "25:00"},
        {"kind": "weird", "text": "что-то", "who": "boss", "date": "вчера"},
        {"kind": "busy", "text": "Футбол", "who": "me", "date": "2026-10-23", "weekly": 3},
        {"kind": "buy", "text": ""},
        {"kind": "task", "text": "старое", "date": "2020-01-01"},
    ]}, ensure_ascii=False)
    items = parser.from_model(raw, THU)
    assert items[0].text == "Вкрутить лампочку" and items[0].time is None and items[0].who == "partner"
    assert items[1].kind == "task" and items[1].who == "me" and items[1].date is None
    assert items[2].weekly == 3 and items[2].date is None
    assert len(items) == 4 and items[3].date is None


def test_parse_without_keys_uses_rules_and_flips_forwarded():
    items = parser.parse("я вкручу лампочку завтра", THU, forwarded="Дима")
    assert items[0].who == "partner"


def test_human_date():
    assert parser.human_date("2026-10-22", today=THU) == "сегодня"
    assert parser.human_date("2026-10-23", "19:00", today=THU) == "завтра в 19:00"
    assert parser.human_date("2026-10-27", today=THU) == "вт 27.10"
