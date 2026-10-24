"""What becomes an item and what doesn't, dates vs times, and the text model failing or being slow."""
import asyncio
import datetime as dt
import json
import time
from types import SimpleNamespace

import pytest

import db
import parser
import planner

THU = dt.date(2026, 10, 22)


# ---------- small talk is not a task ----------

@pytest.mark.parametrize("text", ["Привет", "Спасибо", "Как дела?", "Привет, как дела?", "спасибо большое", "Ок 👍",
                                  "👍", "Ты купил хлеб?", "ты тут?", "Люблю тебя"])
def test_small_talk_is_not_an_item(text):
    assert parser.intent(text, THU) == "chat"
    assert parser.parse(text, THU) == []


def test_small_talk_creates_nothing_and_pings_nobody(couple):
    for text in ("Привет", "Спасибо", "Как дела?"):
        assert planner.add_text(1, text) == ([], [])
    assert not db.items(couple)


@pytest.mark.parametrize("text", ["Лампочка", "Молоко", "давай", "купи"])
def test_fragments_are_asked_about_not_guessed(text):
    """One word without a verb: the bot asks «это что?» with buttons instead of saving a guess."""
    assert parser.intent(text, THU) == "unclear"
    assert parser.parse(text, THU) == []


def test_buy_milk():
    assert [(i.kind, i.text) for i in parser.parse("Купи молоко", THU)] == [("buy", "Молоко")]


def test_remind_to_buy_milk_tomorrow_is_a_dated_task():
    """«Напомни …» with a date stays a task (the list and the morning digest remind about it), not a bare buy line."""
    [it] = parser.parse("Напомни купить молоко завтра", THU)
    assert (it.kind, it.text, it.who, it.date, it.time) == ("task", "Купить молоко", "me", "2026-10-23", None)


def test_lets_buy_groceries_tomorrow_is_for_both():
    [it] = parser.parse("Давай завтра купим продукты", THU)
    assert (it.kind, it.who, it.date) == ("task", "both", "2026-10-23") and "продукты" in it.text


def test_greeting_in_front_of_an_errand_is_dropped():
    assert [(i.kind, i.text) for i in parser.parse("Привет! Купи хлеба", THU)] == [("buy", "Хлеба")]


def test_a_question_with_an_ask_is_an_errand():
    [it] = parser.parse("Можешь завтра забрать посылку?", THU)
    assert (it.kind, it.text, it.who, it.date) == ("task", "Забрать посылку", "partner", "2026-10-23")


def test_three_word_phrases_keep_working():
    assert parser.parse("я помою окна", THU)[0].kind == "task"
    assert parser.parse("вкрутить лампочку", THU)[0].text == "Вкрутить лампочку"


# ---------- dates are not times ----------

def test_day_month_is_a_date_not_a_time():
    [it] = parser.parse("купить подарок маме 05.11", THU)
    assert it.date == "2026-11-05" and it.time is None and "05" not in it.text
    [it] = parser.parse("Ты обещал повесить полку 05.11", THU)
    assert it.date == "2026-11-05" and it.time is None


def test_times_with_colon_or_impossible_month():
    [it] = parser.parse("забрать посылку в 18:30", THU)
    assert it.time == "18:30" and it.date is None
    [it] = parser.parse("в 18.30 созвон с мамой", THU)
    assert it.time == "18:30" and it.text == "Созвон с мамой"
    [it] = parser.parse("в 10.11 едем к маме", THU)  # 11 can be a month: a date, no time invented
    assert it.date == "2026-11-10" and it.time is None


def test_no_time_is_invented():
    for text in ("оплатить интернет в пятницу", "вынести мусор 30.10", "позвонить маме завтра"):
        assert parser.parse(text, THU)[0].time is None, text


def test_month_and_year_edges():
    dec31 = dt.date(2026, 12, 31)
    assert parser.parse("завтра забрать посылку", dec31)[0].date == "2027-01-01"
    assert parser.parse("послезавтра забрать посылку", dec31)[0].date == "2027-01-02"
    assert parser.parse("продлить страховку 5.01", dec31)[0].date == "2027-01-05"
    assert parser.parse("забрать документы 31.12", dec31)[0].date == "2026-12-31"  # today, not next year
    assert parser.parse("забрать документы 31.10", dt.date(2026, 10, 30))[0].date == "2026-10-31"
    assert parser.parse("в понедельник позвонить сантехнику", dt.date(2026, 10, 31))[0].date == "2026-11-02"
    assert parser.parse("оплатить 29.02 налог", dt.date(2026, 10, 1))[0].date is None  # no 29 Feb in 2027


def test_next_saturday_is_the_nearest_one():
    """Current rule, kept on purpose: «в следующую субботу» = the nearest coming Saturday."""
    assert parser.parse("в следующую субботу едем к маме", THU)[0].date == "2026-10-24"


def test_reminder_comes_at_the_said_time_only(couple, monkeypatch):
    monkeypatch.setattr(db, "local_now", lambda: dt.datetime(2026, 10, 22, 9, 0))
    rows, _ = planner.add_text(1, "Ты обещал забрать посылку 23.10 в 18:00")
    assert rows[0]["day"] == "2026-10-23" and rows[0]["time"] == "18:00"
    assert planner.due(dt.datetime(2026, 10, 23, 17, 59)) == []
    assert [o.chat_id for o in planner.due(dt.datetime(2026, 10, 23, 18, 0))] == [2]
    assert planner.due(dt.datetime(2026, 10, 23, 18, 1)) == []  # once
    rows, _ = planner.add_text(1, "Ты обещал купить подарок 05.11")
    assert rows[0]["time"] is None
    assert not [o for o in planner.due(dt.datetime(2026, 11, 5, 5, 11)) if "подарок" in o.text]


# ---------- the text model ----------

@pytest.fixture
def model_on(monkeypatch):
    fake = SimpleNamespace(ai_enabled=True, yandex_text_model="m", yandex_folder_id="f", yandex_api_key="k",
                           ai_daily_limit=3)
    monkeypatch.setattr(parser, "config", fake)
    monkeypatch.setattr(planner, "config", SimpleNamespace(**{**planner.config.__dict__, "ai_enabled": True,
                                                               "ai_daily_limit": 3}))
    return fake


def answer(items):
    return json.dumps({"items": items}, ensure_ascii=False)


def test_model_timeout_falls_back_to_rules(model_on, monkeypatch):
    def slow(*a):
        raise TimeoutError("model timed out")
    monkeypatch.setattr(parser, "_yandex", slow)
    [it] = parser.parse("Ты обещал вкрутить лампочку до субботы", THU)
    assert (it.kind, it.who, it.date) == ("task", "partner", "2026-10-24")


def test_model_garbage_falls_back_to_rules(model_on, monkeypatch):
    monkeypatch.setattr(parser, "_yandex", lambda *a: "не JSON вовсе")
    assert parser.parse("купить хлеб", THU)[0].kind == "buy"
    monkeypatch.setattr(parser, "_yandex", lambda *a: "[1, 2]")
    assert parser.parse("купить хлеб", THU)[0].kind == "buy"


def test_model_is_not_asked_about_small_talk(model_on, monkeypatch):
    calls = []
    monkeypatch.setattr(parser, "_yandex", lambda *a: calls.append(a) or answer([]))
    assert parser.parse("Спасибо", THU) == [] and not calls


def test_model_empty_answer_means_nothing_to_save(model_on, monkeypatch):
    monkeypatch.setattr(parser, "_yandex", lambda *a: answer([]))
    assert parser.parse("как прошёл день у мамы", THU) == []


def test_model_invented_time_is_dropped(model_on, monkeypatch):
    monkeypatch.setattr(parser, "_yandex", lambda *a: answer([{"kind": "task", "text": "Позвонить маме",
                                                               "date": "2026-10-23", "time": "10:00"}]))
    [it] = parser.parse("позвонить маме завтра", THU)
    assert it.date == "2026-10-23" and it.time is None


async def test_slow_model_does_not_block_the_bot(couple, model_on, monkeypatch):
    def slow(*a):
        time.sleep(0.6)  # a blocking network call
        return answer([{"kind": "task", "text": "Вкрутить лампочку", "who": "partner"}])
    monkeypatch.setattr(parser, "_yandex", slow)
    ticks = []

    async def other_work():
        for _ in range(10):
            ticks.append(1)
            await asyncio.sleep(0.03)

    (rows, notes), _ = await asyncio.gather(planner.add_text_async(1, "Ты обещал вкрутить лампочку"), other_work())
    assert len(ticks) == 10  # the loop kept running while the model was thinking
    assert [r["text"] for r in rows] == ["Вкрутить лампочку"] and len(db.items(couple)) == 1
    assert [n.chat_id for n in notes] == [2]


async def test_daily_model_limit_then_rules(couple, model_on, monkeypatch):
    calls = []
    monkeypatch.setattr(parser, "_yandex", lambda *a: calls.append(1) or answer([{"kind": "task", "text": "Дело"}]))
    for _ in range(5):
        rows, _ = await planner.add_text_async(1, "вкрутить лампочку в ванной")
        assert rows
    assert len(calls) == 3  # AI_DAILY_LIMIT=3, the rest went through the rules
