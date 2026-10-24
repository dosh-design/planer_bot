"""Monthly payments, yearly dates and projects («как в семейном планировщике»)."""
import datetime as dt
import json
from contextlib import closing

import pytest

import bot
import db
import parser
import planner
from test_bot import message
from test_web import client, init_data  # noqa: F401  (client is a fixture)

NOW = dt.datetime(2026, 10, 24, 9, 0)  # Saturday
SAT = NOW.date()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    t = {"now": NOW}
    monkeypatch.setattr(db, "local_now", lambda: t["now"])
    return t


def add(uid, text):
    rows, notes = planner.add_text(uid, text, allow_model=False)
    return rows


# ---------- parsing ----------

@pytest.mark.parametrize("phrase, text, repeat, day", [
    ("21-го числа каждого месяца передать показания счётчика", "Передать показания счётчика", "month:21", "2026-11-21"),
    ("Оплатить интернет каждый месяц 5-го", "Оплатить интернет", "month:5", "2026-11-05"),
    ("Ежемесячно 31 числа платить ипотеку", "Платить ипотеку", "month:31", "2026-10-31"),
    ("Кружок Серёже оплачивать 24 числа каждого месяца", "Кружок Серёже оплачивать", "month:24", "2026-10-24"),
])
def test_monthly(phrase, text, repeat, day):
    it, = parser.rules(phrase, SAT)
    assert (it.kind, it.text, it.repeat, it.date) == ("task", text, repeat, day)


@pytest.mark.parametrize("phrase, text, day", [
    ("День рождения мамы 1 февраля", "День рождения мамы", "2027-02-01"),
    ("Годовщина свадьбы 12.06", "Годовщина свадьбы", "2027-06-12"),
    ("ДР Серёжи 1 марта", "ДР Серёжи", "2027-03-01"),
])
def test_yearly(phrase, text, day):
    it, = parser.rules(phrase, SAT)
    assert (it.kind, it.text, it.who, it.repeat, it.date) == ("event", text, "both", "year", day)


def test_birthday_without_date_is_a_plain_plan():
    it, = parser.rules("День рождения у Серёжи", SAT)
    assert it.repeat is None


def test_month_ends():
    assert parser.next_repeat("2026-01-31", "month:31") == "2026-02-28"
    assert parser.next_repeat("2026-02-28", "month:31") == "2026-03-31"  # the anchor, not the last date
    assert parser.prev_repeat("2026-03-31", "month:31") == "2026-02-28"
    assert parser.next_repeat("2028-02-29", "year") == "2029-02-28"


def test_project_steps():
    items = parser.rules("Ремонт кухни: купить плитку, вызвать мастера и выбрать цвет стен", SAT)
    assert [(i.kind, i.text, i.project) for i in items] == [
        ("task", "Купить плитку", "Ремонт кухни"), ("task", "Вызвать мастера", "Ремонт кухни"),
        ("task", "Выбрать цвет стен", "Ремонт кухни")]
    items = parser.rules("Поездка в Карелию: выбрать даты, купить билеты до 10.11", SAT)
    assert [(i.text, i.who, i.date) for i in items] == [("Выбрать даты", "me", None), ("Купить билеты", "me", "2026-11-10")]


@pytest.mark.parametrize("phrase", ["Купить: молоко, хлеб", "Напомни: завтра врач", "встреча в 18:30"])
def test_not_a_project(phrase):
    assert all(i.project is None for i in parser.rules(phrase, SAT))


def test_model_fields_are_checked():
    raw = json.dumps({"items": [
        {"kind": "task", "text": "Показания счётчика", "date": "2026-11-21", "repeat": "month"},
        {"kind": "event", "text": "ДР мамы", "date": "2027-02-01", "repeat": "year"},
        {"kind": "buy", "text": "Молоко", "repeat": "month", "project": "Кухня"},
        {"kind": "task", "text": "Вызвать мастера", "project": "ремонт кухни"},
    ]})
    a, b, c, d = parser.from_model(raw, SAT, "много всего")
    assert (a.repeat, b.repeat, c.repeat, c.project, d.project) == ("month:21", "year", None, None, "Ремонт кухни")


def test_model_missing_monthly_is_read_by_code():
    raw = json.dumps({"items": [{"kind": "task", "text": "Передать показания", "date": None}]})
    it, = parser.from_model(raw, SAT, "21-го числа каждого месяца передать показания")
    assert (it.repeat, it.date) == ("month:21", "2026-11-21")


# ---------- monthly payments ----------

def test_payment_moves_on_and_back(couple):
    row, = add(1, "21-го числа каждого месяца передать показания счётчика")
    assert planner.when_text(row, SAT) == "сб 21.11 · каждый месяц"
    ok, notes = planner.done(1, row["id"])
    done = db.item(row["id"], couple)
    nxt = db.series_open(couple, row["id"])
    assert ok and notes and done["status"] == "done" and done["day"] == "2026-11-21"  # November stays in history
    assert nxt["day"] == "2026-12-21" and nxt["id"] != row["id"]
    assert planner.undo_done(1, row["id"])
    assert db.item(row["id"], couple)["status"] == "open" and db.item(nxt["id"], couple) is None
    assert not planner.undo_done(1, row["id"])  # nothing left to undo


def test_overdue_payment_burns(couple, clock):
    row, = add(1, "Оплатить интернет каждый месяц 5-го")
    clock["now"] = dt.datetime(2026, 11, 7, 9, 0)
    row = db.item(row["id"], couple)
    assert planner.is_hot(row, clock["now"].date())
    assert planner.when_text(row, clock["now"].date()) == "просрочено на 2 дня · каждый месяц"


def test_moving_one_payment_keeps_the_series(couple):
    row, = add(1, "Оплатить интернет каждый месяц 5-го")
    assert planner.edit(1, row["id"], {"day": "2026-11-07"}) == (True, "")  # this month paid later
    assert db.item(row["id"], couple)["repeat"] == "month:5"
    planner.done(1, row["id"])
    assert db.series_open(couple, row["id"])["day"] == "2026-12-05"  # the series still goes by the 5th


# ---------- yearly dates ----------

def test_birthday_week_ahead_and_on_the_day(couple):
    add(1, "День рождения мамы 31 октября")
    out = {o.chat_id: o.text for o in planner.morning(SAT)}
    assert "Через неделю: День рождения мамы" in out[1] and "Через неделю" in out[2]
    out = {o.chat_id: o.text for o in planner.morning(dt.date(2026, 10, 31))}
    assert "День рождения мамы" in out[1] and "Через неделю" not in out[1]


def test_birthday_rolls_to_next_year(couple, clock):
    row, = add(1, "Годовщина свадьбы 26.10")
    clock["now"] = dt.datetime(2026, 10, 27, 9, 0)
    db.roll_yearly(clock["now"].date())
    row = db.item(row["id"], couple)
    assert row["day"] == "2027-10-26" and row["status"] == "open"
    assert not planner.is_hot(row, clock["now"].date())


# ---------- projects ----------

def test_project_from_a_phrase_is_a_real_project(couple):
    rows = add(1, "Ремонт кухни: купить плитку, вызвать мастера, выбрать цвет стен")
    pid = rows[0]["project_id"]
    assert pid and {r["project_id"] for r in rows} == {pid}
    assert db.project(pid, couple)["name"] == "Ремонт кухни"
    add(2, "ремонт кухни: заказать фартук")  # the same project, whoever writes and in any case
    planner.done(1, rows[0]["id"])
    pr, = db.projects(couple)
    assert (pr["name"], pr["done"], pr["total"], pr["open"]) == ("Ремонт кухни", 1, 4, 3)


def test_project_steps_dont_nag(couple):
    rows = add(1, "Ремонт кухни: вызвать мастера")
    with closing(db._connect()) as c:
        c.execute("UPDATE items SET created = '2026-10-01 10:00'")
    row = db.item(rows[0]["id"], couple)
    assert not planner.is_hot(row, SAT)
    assert row["id"] not in [r["id"] for r in db.nudge_candidates(SAT)]


def test_create_edit_delete_project(couple):
    pid, err = planner.create_project(1, "  Отпуск ", "Карелия в июле")
    assert pid and not err
    assert planner.create_project(2, "отпуск")[1] == "Проект с таким названием уже есть"
    assert planner.create_project(1, " ")[1] == "Назовите проект"
    rows, notes, err = planner.add_to_project(1, pid, "купить билеты до 10.11", "partner")
    assert not err and rows[0]["kind"] == "task" and rows[0]["text"] == "Купить билеты" and rows[0]["day"] == "2026-11-10"
    assert rows[0]["assignee_id"] == 2 and notes and notes[0].chat_id == 2  # Дима hears about his new step
    assert planner.edit_project(1, pid, {"name": "Отпуск в Карелии", "about": ""}) == (True, "")
    assert db.project(pid, couple)["name"] == "Отпуск в Карелии"
    assert planner.delete_project(1, pid)
    assert db.project(pid, couple) is None
    row = db.item(rows[0]["id"], couple)
    assert row["status"] == "open" and row["project_id"] is None  # the step stays, as an ordinary task


def test_projects_are_private_to_the_couple(couple):
    pid, _ = planner.create_project(1, "Отпуск")
    db.save_user(5, "Чужой")
    db.couple_code(5)
    assert planner.add_to_project(5, pid, "украсть билеты")[2] == "Проект не найден"
    assert planner.edit_project(5, pid, {"name": "Моё"}) == (False, "Проект не найден")
    assert not planner.delete_project(5, pid)
    rows = add(5, "Позвонить маме")
    assert not planner.move_to_project(5, rows[0]["id"], pid)


def test_move_task_in_and_out(couple):
    pid, _ = planner.create_project(1, "Дом и ремонт")
    row, = add(1, "Повесить полку")
    assert planner.move_to_project(2, row["id"], pid)
    assert db.item(row["id"], couple)["project_id"] == pid
    assert planner.move_to_project(2, row["id"], None)
    buy, = add(1, "купить молоко")
    assert not planner.move_to_project(1, buy["id"], pid)  # shopping stays in shopping


def test_solo_projects_follow_into_the_couple():
    db.save_user(1, "Маша")
    db.save_user(2, "Дима")
    planner.add_text(1, "Отпуск: купить крем", allow_model=False)
    planner.add_text(2, "Отпуск: оформить страховку", allow_model=False)
    planner.add_text(2, "Дача: покрасить забор", allow_model=False)
    assert db.join(2, db.invite_code(1)) == "ok"
    cid = db.user(1)["couple_id"]
    names = {pr["name"]: pr["total"] for pr in db.projects(cid)}
    assert names == {"Отпуск": 2, "Дача": 1}  # same names merged, nothing lost


def test_old_base_without_projects_migrates():
    from contextlib import closing as cl
    import sqlite3
    path = db.config.data_dir / "family.sqlite3"
    path.unlink()
    with cl(sqlite3.connect(path)) as c:
        c.executescript("""CREATE TABLE items (id INTEGER PRIMARY KEY AUTOINCREMENT, couple_id INTEGER NOT NULL,
            kind TEXT NOT NULL, text TEXT NOT NULL, author_id INTEGER NOT NULL, assignee_id INTEGER, day TEXT, time TEXT,
            weekday INTEGER, status TEXT NOT NULL DEFAULT 'open', created TEXT NOT NULL, done_at TEXT,
            reminded INTEGER NOT NULL DEFAULT 0, nudges INTEGER NOT NULL DEFAULT 0, last_nudge TEXT);
            INSERT INTO items (couple_id, kind, text, author_id, created) VALUES (1, 'task', 'Старое', 1, '2026-10-01 10:00');""")
        c.commit()
    db.init()
    row = db.item(1, 1)
    assert row["text"] == "Старое" and row["project_id"] is None and row["repeat"] is None
    assert db.projects(1) == []


def test_same_purchase_is_not_listed_twice(couple):
    first, = add(1, "купить молоко")
    again, = add(2, "Купить Молоко")
    assert again["id"] == first["id"] and len(db.items(couple, "buy")) == 1
    _, notes = planner.add_text(2, "купить молоко", allow_model=False)
    assert notes == []  # nothing new, nobody is told


async def test_bot_list_and_projects_command(couple):
    add(1, "Ремонт кухни: купить плитку, вызвать мастера")
    m = message(1, "Маша", "📋 Дела")
    await bot.tasks(m)
    assert "📁 Ремонт кухни · 0 из 2" in m.answers[0][0]
    m = message(1, "Маша", "/projects")
    await bot.projects_list(m)
    assert "Ремонт кухни" in m.answers[0][0] and "Вызвать мастера" in m.answers[0][0]


async def test_api_projects(client, couple):  # noqa: F811
    h = {"X-Init-Data": init_data(1, "Маша")}
    r = await client.post("/api/projects", json={"name": "Подготовка к празднику", "about": "НГ у нас"}, headers=h)
    pr = (await r.json())["project"]
    assert r.status == 200 and pr["about"] == "НГ у нас" and pr["total"] == 0
    r = await client.post(f"/api/projects/{pr['id']}/items", json={"text": "Купить ёлку 28.12", "who": "both"}, headers=h)
    step, = (await r.json())["project"]["items"]
    assert step["text"] == "Купить ёлку" and step["assignee"] is None and step["project"] == "Подготовка к празднику"
    await client.post(f"/api/items/{step['id']}/done", headers=h)
    got = (await (await client.get(f"/api/projects/{pr['id']}", headers=h)).json())["project"]
    assert (got["done"], got["total"], got["items"][0]["status"]) == (1, 1, "done")
    state = await (await client.get("/api/state", headers=h)).json()
    assert state["projects"][0]["name"] == "Подготовка к празднику"
    r = await client.post("/api/projects", json={"name": "подготовка к празднику"}, headers=h)
    assert r.status == 400
    other = {"X-Init-Data": init_data(9, "Чужой")}
    assert (await client.get(f"/api/projects/{pr['id']}", headers=other)).status == 404
    assert (await client.delete(f"/api/projects/{pr['id']}", headers=other)).status == 404
    assert (await client.get(f"/api/projects/{pr['id']}")).status == 401
    task = (await (await client.post("/api/add", json={"text": "Повесить гирлянду"}, headers=h)).json())["items"][0]
    r = await client.patch(f"/api/items/{task['id']}", json={"project_id": pr["id"]}, headers=h)
    assert r.status == 200 and (await r.json())["item"]["project"] == "Подготовка к празднику"
    assert (await client.patch(f"/api/items/{task['id']}", json={"project_id": "x"}, headers=h)).status == 400
    assert (await client.delete(f"/api/projects/{pr['id']}", headers=h)).status == 200
    state = await (await client.get("/api/state", headers=h)).json()
    assert state["projects"] == [] and any(i["text"] == "Повесить гирлянду" and i["project_id"] is None for i in state["items"])


async def test_api_state_fields(client, couple):  # noqa: F811
    h = {"X-Init-Data": init_data(1, "Маша")}
    await client.post("/api/add", json={"text": "21-го каждого месяца передать показания"}, headers=h)
    state = await (await client.get("/api/state", headers=h)).json()
    pay, = [i for i in state["items"] if i["repeat"]]
    assert pay["repeat"] == "month:21" and pay["when"].endswith("каждый месяц")
    await client.post(f"/api/items/{pay['id']}/done", headers=h)
    state = await (await client.get("/api/state", headers=h)).json()
    assert [i["day"] for i in state["items"] if i["text"] == pay["text"]] == ["2026-12-21"]  # the next instance
