"""Busy time spans («с 8 до 10») and weekly house chores («по вторникам мусор — на тебе»)."""
import datetime as dt
import json
import sqlite3
from contextlib import closing

import pytest

import bot
import db
import parser
import planner
from test_bot import message
from test_web import client, init_data  # noqa: F401  (client is a fixture)

NOW = dt.datetime(2026, 10, 24, 9, 0)  # Saturday morning
SAT = NOW.date()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    t = {"now": NOW}
    monkeypatch.setattr(db, "local_now", lambda: t["now"])
    return t


# ---------- spans ----------

@pytest.mark.parametrize("phrase, span", [
    ("с восьми до 10 утра", ("08:00", "10:00")),
    ("с 8 до 10", ("08:00", "10:00")),
    ("с 8:30 до 10", ("08:30", "10:00")),
    ("с 7 до 9 вечера", ("19:00", "21:00")),
    ("с семи до девяти вечера", ("19:00", "21:00")),
    ("с 11 до 2 дня", ("11:00", "14:00")),
    ("с 10 до 2", ("10:00", "14:00")),
    ("с 3 до 5", ("15:00", "17:00")),
    ("с трёх до пяти", ("15:00", "17:00")),
    ("с полудня до двух", ("12:00", "14:00")),
    ("в 19:00-21:00", ("19:00", "21:00")),
    ("8–10 утра", ("08:00", "10:00")),
])
def test_span(phrase, span):
    assert parser.find_span(f" {phrase.replace('ё', 'е')} ") == span


@pytest.mark.parametrize("phrase", ["купить 2-3 яблока", "с понедельника до пятницы", "отпуск 05.11-10.11"])
def test_not_a_span(phrase):
    assert parser.find_span(f" {phrase} ") is None


def test_the_reported_phrase():
    """«Завтра в воскресенье пробежка с восьми до 10 утра» was saved as «Пробежка · завтра», no time."""
    it, = parser.rules("Завтра в воскресенье пробежка с восьми до 10 утра", SAT)
    assert (it.kind, it.text, it.date, it.time, it.end) == ("busy", "Пробежка", "2026-10-25", "08:00", "10:00")


def test_hour_in_words():
    it, = parser.rules("позвонить маме в восемь вечера", SAT)
    assert (it.text, it.time, it.end) == ("Позвонить маме", "20:00", None)


def test_model_span_is_checked_and_completed_by_code():
    raw = json.dumps({"items": [{"kind": "busy", "text": "Пробежка", "date": "2026-10-25", "time": None}]})
    it, = parser.from_model(raw, SAT, "в воскресенье пробежка с восьми до 10 утра")
    assert (it.time, it.end) == ("08:00", "10:00")
    raw = json.dumps({"items": [{"kind": "busy", "text": "Баня", "date": "2026-10-25", "time": "18:00", "end": "17:00"}]})
    it, = parser.from_model(raw, SAT, "в воскресенье баня в 18")
    assert (it.time, it.end) == ("18:00", None)  # an end before the start is dropped


def test_span_is_saved_and_shown(couple):
    rows, _ = planner.add_text(1, "Завтра пробежка с восьми до 10 утра", allow_model=False)
    assert rows[0]["end_time"] == "10:00"
    assert planner.when_text(rows[0], SAT) == "завтра, 08:00–10:00"
    rows, _ = planner.add_text(1, "По четвергам у меня футбол с 19 до 21", allow_model=False)
    assert planner.when_text(rows[0], SAT) == "каждый четверг, 19:00–21:00"


def test_edit_span(couple):
    rows, _ = planner.add_text(1, "Завтра пробежка с 8 до 10", allow_model=False)
    iid = rows[0]["id"]
    assert planner.edit(1, iid, {"end": "07:00"}) == (False, "Конец должен быть позже начала")
    assert planner.edit(1, iid, {"end": "11:30"}) == (True, "")
    assert db.item(iid, couple)["end_time"] == "11:30"
    assert planner.edit(1, iid, {"time": None}) == (True, "")  # no start, no span
    assert db.item(iid, couple)["end_time"] is None
    assert planner.edit(1, iid, {"end": "12:00"}) == (False, "Укажите, со скольки")


def test_old_base_gets_the_column_and_keeps_its_rows(tmp_path, monkeypatch):
    path = db.config.data_dir / "family.sqlite3"
    path.unlink()
    with closing(sqlite3.connect(path)) as c:
        c.executescript("""CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, couple_id INTEGER NOT NULL,
            kind TEXT NOT NULL, text TEXT NOT NULL, author_id INTEGER NOT NULL, assignee_id INTEGER, day TEXT, time TEXT,
            weekday INTEGER, status TEXT NOT NULL DEFAULT 'open', created TEXT NOT NULL, done_at TEXT,
            reminded INTEGER NOT NULL DEFAULT 0, nudges INTEGER NOT NULL DEFAULT 0, last_nudge TEXT);
            INSERT INTO items (couple_id, kind, text, author_id, created) VALUES (1, 'task', 'Старое дело', 1, '2026-10-01 10:00');""")
        c.commit()
    db.init()
    db.init()  # twice is harmless
    row = db.item(1, 1)
    assert row["text"] == "Старое дело" and row["end_time"] is None


async def test_api_span(client, couple):  # noqa: F811
    h = {"X-Init-Data": init_data(1, "Маша")}
    r = await client.post("/api/add", json={"text": "Завтра пробежка с 8 до 10"}, headers=h)
    item = (await r.json())["items"][0]
    assert item["end"] == "10:00" and item["when"] == "завтра, 08:00–10:00"
    r = await client.patch(f"/api/items/{item['id']}", json={"day": "2026-10-25", "time": "09:00", "end": "11:00"}, headers=h)
    assert r.status == 200 and (await r.json())["item"]["when"] == "завтра, 09:00–11:00"
    r = await client.patch(f"/api/items/{item['id']}", json={"end": "08:00"}, headers=h)
    assert r.status == 400


# ---------- weekly house chores ----------

def _chore(couple, text="По вторникам выносить мусор — на тебе"):
    rows, _ = planner.add_text(1, text, allow_model=False)
    return rows[0]


def test_chore_is_parsed_with_whose_turn(couple):
    row = _chore(couple)
    assert (row["kind"], row["text"], row["assignee_id"], row["weekday"]) == ("task", "Выносить мусор", 2, 1)
    assert planner.when_text(row, SAT) == "каждый вторник"
    assert planner.line(row, SAT).startswith("🏠 Выносить мусор · Дима")


def test_chore_done_stays_for_next_week(couple, clock):
    row = _chore(couple)
    tue = dt.datetime(2026, 10, 27, 9, 0)
    clock["now"] = tue
    assert planner.when_text(db.item(row["id"], couple), tue.date()) == "сегодня, каждый вторник"
    ok, notes = planner.done(2, row["id"])
    notes = [n for n in notes if not n.text.startswith("🏅")]  # the family's first achievement goes to both
    assert ok and len(notes) == 1 and notes[0].chat_id == 1  # Маша hears the trash is out
    assert planner.done(2, row["id"]) == (True, [])  # a second tick this week tells nobody again
    row = db.item(row["id"], couple)
    assert row["status"] == "open" and planner.chore_done(row, tue.date())
    assert planner.when_text(row, tue.date()) == "сделано, дальше вт 03.11"
    assert planner.chore_done(row, dt.date(2026, 11, 2))  # still done on Monday
    assert not planner.chore_done(row, dt.date(2026, 11, 3))  # the next Tuesday it is due again


def test_chore_undo(couple):
    row = _chore(couple)
    planner.done(2, row["id"])
    assert planner.undo_done(2, row["id"])
    row = db.item(row["id"], couple)
    assert row["status"] == "open" and not planner.chore_done(row, SAT)
    assert not planner.undo_done(2, row["id"])  # nothing to undo


def test_chore_never_burns_or_nags(couple):
    row = _chore(couple)
    with closing(db._connect()) as c:
        c.execute("UPDATE items SET created = '2026-10-01 10:00' WHERE id = ?", (row["id"],))
    row = db.item(row["id"], couple)
    assert not planner.is_hot(row, SAT)
    assert row["id"] not in [r["id"] for r in db.nudge_candidates(SAT)]


def test_morning_names_the_chore_on_its_day(couple):
    _chore(couple)
    tue = dt.date(2026, 10, 27)
    out = {o.chat_id: o.text for o in planner.morning(tue)}
    assert "Выносить мусор" in out[2] and 1 not in out
    assert not planner.morning(dt.date(2026, 10, 28))  # Wednesday: nothing


def test_morning_skips_a_chore_done_early(couple, clock):
    row = _chore(couple)
    clock["now"] = dt.datetime(2026, 10, 27, 8, 0)
    planner.done(2, row["id"])
    assert not planner.morning(dt.date(2026, 10, 27))


async def test_bot_list_has_house_chores_section(couple):
    _chore(couple)
    m = message(1, "Маша", "📋 Дела")
    await bot.tasks(m)
    text, markup = m.answers[0]
    assert "<b>🏠 Обязанности по дому</b>" in text and "Выносить мусор" in text
    assert markup.inline_keyboard[0][0].callback_data.startswith("done:")


async def test_api_chore_flag(client, couple):  # noqa: F811
    h = {"X-Init-Data": init_data(2, "Дима")}
    row = _chore(couple)
    await client.post(f"/api/items/{row['id']}/done", headers=h)
    state = await (await client.get("/api/state", headers=h)).json()
    it, = [i for i in state["items"] if i["id"] == row["id"]]
    assert it["chore_done"] is True and it["weekday"] == 1 and it["when"].startswith("сделано, дальше")


def test_greeting_is_about_sharing_chores():
    assert "обещали" not in bot.HELP and "обязанность по дому" in bot.HELP

