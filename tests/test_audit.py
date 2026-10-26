"""Regression tests for defects found in the full audit (2026-10-25) and the API's behaviour on bad input."""
import datetime as dt
import time
from contextlib import closing

import pytest

import db
import parser
import planner
import webapp_auth
from test_web import client, init_data  # noqa: F401  (client is a fixture)

NOW = dt.datetime(2026, 10, 25, 9, 0)
H1 = {"X-Init-Data": init_data(1, "Маша")}


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    t = {"now": NOW}
    monkeypatch.setattr(db, "local_now", lambda: t["now"])
    return t


# A-1: joining a partner took the newcomer's project stages away (deleted with the old couple by cascade).
def test_join_keeps_project_stages():
    db.save_user(1, "Маша")
    db.couple_code(1)
    db.save_user(2, "Дима")
    db.couple_code(2)  # Дима started alone
    pid, _ = planner.create_project(2, "Ремонт")
    sid, _ = planner.add_section(2, pid, "Демонтаж")
    rows, _, _ = planner.add_to_project(2, pid, "снять плитку", section_id=sid)
    assert db.join(2, db.invite_code(1)) == "ok"
    cid = db.user(1)["couple_id"]
    assert [s["name"] for s in db.sections(pid, cid)] == ["Демонтаж"]
    assert db.item(rows[0]["id"], cid)["section_id"] == sid


def test_join_merging_same_project_keeps_stages():
    db.save_user(1, "Маша")
    db.couple_code(1)
    planner.create_project(1, "Ремонт")
    db.save_user(2, "Дима")
    db.couple_code(2)
    pid2, _ = planner.create_project(2, "ремонт")
    planner.add_section(2, pid2, "Отделка")
    assert db.join(2, db.invite_code(1)) == "ok"
    cid = db.user(1)["couple_id"]
    pr, = db.projects(cid)
    assert [s["name"] for s in db.sections(pr["id"], cid)] == ["Отделка"]


# A-2: a date that does not exist was silently dropped and the bot said «Записал» as if all was fine.
@pytest.mark.parametrize("phrase, bad", [("31.02 оплатить налог", "31.02"), ("позвонить маме 31 июня", "31 июня"),
                                         ("в 25:00 позвонить маме", "в 25:00"), ("встреча 29.02.2027", "29.02.2027")])
def test_nonexistent_date_is_reported(phrase, bad):
    assert parser.bad_when(phrase) == bad


@pytest.mark.parametrize("phrase", ["встреча 29.02", "в 18:30 ужин", "21.11 показания", "в 10.15 созвон", "5 мая"])
def test_real_dates_are_not_reported(phrase):
    assert parser.bad_when(phrase) is None


async def test_app_warns_about_nonexistent_date(client, couple):  # noqa: F811
    r = await (await client.post("/api/add", json={"text": "оплатить налог 31.02"}, headers=H1)).json()
    it, = r["items"]
    assert it["day"] is None and "31.02" in it["warnings"][0]


# A-3: after downtime every missed reminder went out at once, even days late.
def test_stale_reminder_is_not_sent_late(couple, clock):
    rows, _ = planner.add_text(1, "Ты обещал позвонить в банк", allow_model=False)
    db.update_item(rows[0]["id"], couple, day="2026-10-25", time="10:00")
    clock["now"] = dt.datetime(2026, 10, 26, 9, 0)  # the bot was down all day
    assert not planner.due(clock["now"])
    assert db.item(rows[0]["id"], couple)["reminded"] == 1  # and it won't come later either


def test_slightly_late_reminder_still_goes(couple):
    rows, _ = planner.add_text(1, "Ты обещал позвонить в банк", allow_model=False)
    db.update_item(rows[0]["id"], couple, day="2026-10-25", time="10:00")
    assert planner.due(dt.datetime(2026, 10, 25, 11, 0))  # a restart of a few minutes or an hour


# A-4: launch data stamped in the future, or with a user that is not an object, was accepted / crashed.
def _signed(fields):
    import hashlib
    import hmac
    from urllib.parse import urlencode
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", b"123:test", hashlib.sha256).digest()
    return urlencode(fields | {"hash": hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()})


def test_init_data_from_the_future_is_rejected():
    future = _signed({"auth_date": str(int(time.time()) + 3600), "user": '{"id": 1}'})
    assert webapp_auth.verify(future, "123:test") is None
    assert webapp_auth.verify(_signed({"auth_date": str(int(time.time())), "user": "7"}), "123:test") is None
    assert webapp_auth.verify(_signed({"auth_date": str(int(time.time())), "user": '{"id": 1}'}), "123:test")


# The API answers bad input with a 4xx and a message, never a 500, and changes nothing.
BAD_BODIES = [None, "", "[]", "null", '"x"', "{", '{"text": null}', '{"text": 5}', '{"day": "2026-13-01"}',
              '{"time": "99:99"}', '{"remind": "x"}', '{"remind": true}', '{"repeat": "hour"}', '{"section": "1"}',
              '{"project_id": "x"}', '{"who": "boss"}', '{"dir": 5}', '{"name": ""}', '{"start": "x"}']


async def test_api_bad_input_never_500(client, couple):  # noqa: F811
    rows, _ = planner.add_text(1, "Ты обещал вкрутить лампочку", allow_model=False)
    iid = rows[0]["id"]
    pid, _ = planner.create_project(1, "Ремонт")
    sid, _ = planner.add_section(1, pid, "Этап")
    before = [dict(r) for r in db.items(couple)]
    routes = [("POST", "/api/add"), ("POST", f"/api/items/{iid}/who"), ("PATCH", f"/api/items/{iid}"),
              ("POST", f"/api/items/{iid}/move"), ("POST", "/api/settings"), ("POST", "/api/projects"),
              ("PATCH", f"/api/projects/{pid}"), ("POST", f"/api/projects/{pid}/items"),
              ("POST", f"/api/projects/{pid}/sections"), ("PATCH", f"/api/sections/{sid}")]
    for method, path in routes:
        for body in BAD_BODIES:
            r = await client.request(method, path, data=body, headers=H1 | {"Content-Type": "application/json"})
            assert r.status < 500, (method, path, body, r.status, await r.text())
    for path in ("/api/items/abc/done", "/api/projects/abc", "/api/items/999999/done", "/api/sections/999/x"):
        assert (await client.post(path, headers=H1)).status in (404, 405)
    assert [dict(r) for r in db.items(couple)] == before


# One request repeated (a double tap, a retry) does not make a second item or a second point.
async def test_repeated_requests_do_not_duplicate(client, couple):  # noqa: F811
    for _ in range(2):
        await client.post("/api/add", json={"text": "купить молоко"}, headers=H1)
    assert len([r for r in db.items(couple, "buy") if r["text"] == "Молоко"]) == 1
    rows, _ = planner.add_text(1, "Ты обещал вкрутить лампочку", allow_model=False)
    a = await (await client.post(f"/api/items/{rows[0]['id']}/done", headers=H1)).json()
    b = await (await client.post(f"/api/items/{rows[0]['id']}/done", headers=H1)).json()
    assert a["scored"] and not b["scored"] and b["score"]["family"] == 1


# Data survives a restart: the base is a file, and init() on an existing base keeps every row.
def test_data_survives_restart(couple):
    pid, _ = planner.create_project(1, "Отпуск")
    rows, _, _ = planner.add_to_project(1, pid, "купить билеты до 10.11", "partner")
    planner.edit(1, rows[0]["id"], {"note": "Аэрофлот", "remind": 1440})
    snapshot = dict(db.item(rows[0]["id"], couple))
    db.init()  # what main.py does on every start
    assert dict(db.item(rows[0]["id"], couple)) == snapshot
    assert db.projects(couple)[0]["name"] == "Отпуск"


# AI down: the phrase is still saved by the rules, nothing is lost.
def test_model_failure_falls_back_to_rules(couple, monkeypatch):
    monkeypatch.setattr(parser.config.__class__, "ai_enabled", property(lambda self: True))
    monkeypatch.setattr(parser, "_yandex", lambda *a: (_ for _ in ()).throw(TimeoutError()))
    rows, _ = planner.add_text(1, "купить хлеб", allow_model=True)
    assert [r["text"] for r in rows] == ["Хлеб"]


def test_model_garbage_is_not_trusted(couple, monkeypatch):
    monkeypatch.setattr(parser.config.__class__, "ai_enabled", property(lambda self: True))
    monkeypatch.setattr(parser, "_yandex", lambda *a: '{"items": [{"kind": "drop_table", "text": "Вынести мусор",'
                                                      ' "date": "вчера", "time": "77:00", "who": "admin"}]}')
    rows, _ = planner.add_text(1, "вынести мусор", allow_model=True)
    r, = rows
    assert (r["kind"], r["day"], r["time"], r["assignee_id"]) == ("task", None, None, 1)


# Leaving the planner (/delete) removes the person's pending phrase too.
def test_delete_user_forgets_pending_phrase(couple):
    db.kv_set("pending:1", "ключи")
    db.delete_user(1)
    assert not db.kv_get("pending:1")
    with closing(db._connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM users WHERE id = 1").fetchone()[0] == 0


# Shopping between the two: one adds, the other ticks off, both read the same list from the server.
async def test_shopping_between_partners(client, couple):  # noqa: F811
    h2 = {"X-Init-Data": init_data(2, "Дима")}
    r = await (await client.post("/api/add", json={"text": "купить молоко"}, headers=H1)).json()
    iid = r["items"][0]["id"]
    s2 = await (await client.get("/api/state", headers=h2)).json()
    assert [i["text"] for i in s2["items"] if i["kind"] == "buy"] == ["Молоко"]
    assert (await client.post(f"/api/items/{iid}/done", headers=h2)).status == 200
    s1 = await (await client.get("/api/state", headers=H1)).json()
    assert not [i for i in s1["items"] if i["kind"] == "buy"]
    assert [i["text"] for i in s1["done"] if i["kind"] == "buy"] == ["Молоко"]


async def test_bot_warns_about_nonexistent_date(couple):
    import bot
    from test_bot import FakeBot, message
    m = message(1, "Маша", "Ты обещал оплатить налог 31.02")
    await bot.text(m, FakeBot())
    assert m.answers[0][0].startswith("Записал:") and "31.02" in m.answers[-1][0] and "без срока" in m.answers[-1][0]


async def test_bot_callbacks_after_delete_do_not_crash(couple):
    import bot
    from types import SimpleNamespace
    from test_bot import FakeBot
    db.kv_set("pending:1", "ключи")
    db.delete_user(1)
    db.kv_set("pending:1", "ключи")  # the question was asked before /delete
    answered = []

    async def answer(text=None, **kw):
        answered.append(text)
    cb = SimpleNamespace(from_user=SimpleNamespace(id=1), data="force:task", message=None, answer=answer)
    await bot.cb_force(cb, FakeBot())
    cb.data = "forget"
    await bot.cb_forget(cb)
    assert answered == ["Сначала /start", "Удалил"]


# 26.10 from the student: «напоминать в день каждый час» until the task is done.
def test_hourly_reminder_needs_a_date(couple):
    rows, _ = planner.add_text(1, "Ты обещал позвонить в банк", allow_model=False)
    assert planner.edit(1, rows[0]["id"], {"remind": -2})[1] == "Чтобы напоминать каждый час, укажите дату"
    assert planner.edit(1, rows[0]["id"], {"day": "2026-10-25", "remind": -2}) == (True, "")


def test_hourly_reminder_schedule_and_stop(couple):
    rows, _ = planner.add_text(1, "Ты обещал позвонить в банк", allow_model=False)
    iid = rows[0]["id"]
    planner.edit(1, iid, {"day": "2026-10-25", "remind": -2})
    got = []
    for h in range(0, 24):
        for m in (0, 1, 30):
            if [o for o in planner.due(dt.datetime(2026, 10, 25, h, m)) if "банк" in o.text]:
                got.append(h)
        if h == 13:
            planner.done(2, iid)
    m = db.config.morning_hour
    assert got == list(range(m, 14)), got  # from the morning, once an hour, silent after «Сделано»
    assert not [o for o in planner.due(dt.datetime(2026, 10, 26, 12, 0)) if "банк" in o.text]  # only on its day


# 26.10 from the student: the partner hears about every new record at once, not only what lands on them.
@pytest.mark.parametrize("phrase, head", [("Мне надо позвонить в банк завтра", "записал(а) себе"),
                                          ("В субботу у меня баня с 10 до 14", "записал(а) занятость"),
                                          ("Ты обещал вкрутить лампочку", "записал(а) на тебя"),
                                          ("Купить молоко", "добавил(а) в покупки")])
def test_partner_hears_about_every_new_record(couple, phrase, head):
    rows, notes = planner.add_text(1, phrase, allow_model=False)
    to_partner = [n.text for n in notes if n.chat_id == 2]
    assert rows and len(to_partner) == 1 and head in to_partner[0]
    _, again = planner.add_text(1, "Купить молоко", allow_model=False) if "молоко" in phrase.lower() else (None, [])
    assert not again  # the same purchase again is not new: no second ping


def test_project_steps_come_as_one_message(couple):
    _, notes = planner.add_text(1, "Ремонт кухни: купить плитку, вызвать мастера, выбрать цвет", allow_model=False)
    assert len([n for n in notes if n.chat_id == 2]) == 1
