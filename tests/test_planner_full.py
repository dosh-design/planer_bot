"""Full family planner: project stages, notes and order, project dates, reminders ahead of time, repeats with history,
shopping history. One test per scenario of the spec; the API ones go through the same routes as the mini-app."""
import datetime as dt
from contextlib import closing

import pytest

import db
import planner
from test_web import client, init_data  # noqa: F401  (client is a fixture)

NOW = dt.datetime(2026, 10, 24, 9, 0)  # Saturday
SAT = NOW.date()
H1 = {"X-Init-Data": init_data(1, "Маша")}
H3 = {"X-Init-Data": init_data(3, "Чужой")}


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    t = {"now": NOW}
    monkeypatch.setattr(db, "local_now", lambda: t["now"])
    return t


def task(couple, text="Купить плитку", **fields):
    rows, _ = planner.add_text(1, text, allow_model=False)
    if fields:
        db.update_item(rows[0]["id"], couple, **fields)
    return rows[0]["id"]


def reminders(now):
    return [o for o in planner.due(now)]


# 1. A project task: add, edit, done, undo, delete.
def test_project_task_lifecycle(couple):
    pid, _ = planner.create_project(1, "Ремонт")
    rows, _, err = planner.add_to_project(1, pid, "купить плитку", "partner")
    iid = rows[0]["id"]
    assert not err and rows[0]["assignee_id"] == 2
    assert planner.edit(1, iid, {"text": "Купить плитку в ванную", "note": "Белая, 20×20", "day": "2026-10-30"}) == (True, "")
    row = db.item(iid, couple)
    assert (row["text"], row["note"], row["day"]) == ("Купить плитку в ванную", "Белая, 20×20", "2026-10-30")
    assert planner.done(2, iid)[0] and db.item(iid, couple)["status"] == "done"
    assert planner.undo_done(2, iid) and db.item(iid, couple)["status"] == "open"
    assert planner.remove(1, iid) and db.item(iid, couple) is None


# 2. Stages: create, rename, delete without losing tasks.
def test_sections_keep_tasks(couple):
    pid, _ = planner.create_project(1, "Ремонт")
    sid, err = planner.add_section(1, pid, "Демонтаж")
    assert sid and not err
    assert planner.add_section(1, pid, "демонтаж")[1] == "Такой этап уже есть"
    rows, _, _ = planner.add_to_project(1, pid, "снять плитку", section_id=sid)
    assert rows[0]["section_id"] == sid
    assert planner.rename_section(1, sid, "Черновые работы") == (True, "")
    assert planner.delete_section(1, sid)
    row = db.item(rows[0]["id"], couple)
    assert row and row["project_id"] == pid and row["section_id"] is None  # the task stayed in the project


# 3. Order inside a stage.
def test_order_moves_up_and_down(couple):
    pid, _ = planner.create_project(1, "Отпуск")
    ids = [planner.add_to_project(1, pid, t)[0][0]["id"] for t in ("купить билеты", "забронировать отель", "оформить страховку")]
    assert [r["id"] for r in db.project_items(pid, couple)] == ids
    assert planner.move(1, ids[2], -1)
    assert [r["id"] for r in db.project_items(pid, couple)] == [ids[0], ids[2], ids[1]]
    assert not planner.move(1, ids[0], -1)  # already first


# 4. Progress counts done tasks.
def test_progress(couple):
    pid, _ = planner.create_project(1, "Праздник")
    a = planner.add_to_project(1, pid, "заказать торт")[0][0]["id"]
    planner.add_to_project(1, pid, "купить шары")
    planner.done(1, a)
    pr, = db.projects(couple)
    assert (pr["done"], pr["total"]) == (1, 2)


# 5. Project dates are checked.
def test_project_dates(couple):
    pid, _ = planner.create_project(1, "Ремонт")
    assert planner.edit_project(1, pid, {"start": "2026-11-01", "end": "2026-12-15"}) == (True, "")
    pr, = db.projects(couple)
    assert (pr["start"], pr["end"]) == ("2026-11-01", "2026-12-15")
    assert planner.edit_project(1, pid, {"end": "2026-10-01"})[1] == "Конец проекта раньше начала"
    assert planner.edit_project(1, pid, {"start": "завтра"})[1] == "Не понял дату"


# 6. Assignees are only the couple's members, checked on the server; one task, not a copy per person.
async def test_assignee_only_couple_members(client, couple):  # noqa: F811
    pid, _ = planner.create_project(1, "Ремонт")
    rows, _, _ = planner.add_to_project(1, pid, "вызвать мастера", "both")
    iid = rows[0]["id"]
    assert (await client.post(f"/api/items/{iid}/who", json={"who": 999}, headers=H1)).status == 400
    assert (await client.post(f"/api/items/{iid}/who", json={"who": "partner"}, headers=H1)).status == 200
    assert db.item(iid, couple)["assignee_id"] == 2
    with closing(db._connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM items WHERE text = 'Вызвать мастера'").fetchone()[0] == 1


# 7. Another couple can't touch the project, its stages or tasks.
async def test_other_couple_is_locked_out(client, couple):  # noqa: F811
    db.save_user(3, "Чужой")
    db.couple_code(3)
    pid, _ = planner.create_project(1, "Ремонт")
    sid, _ = planner.add_section(1, pid, "Демонтаж")
    iid = planner.add_to_project(1, pid, "снять плитку")[0][0]["id"]
    assert (await client.get(f"/api/projects/{pid}", headers=H3)).status == 404
    assert (await client.patch(f"/api/sections/{sid}", json={"name": "x"}, headers=H3)).status == 404
    assert (await client.patch(f"/api/items/{iid}", json={"note": "x"}, headers=H3)).status == 404
    assert (await client.post(f"/api/items/{iid}/move", json={"dir": 1}, headers=H3)).status == 400
    assert db.item(iid, couple)["note"] == ""


# 8. Project tasks show in Дела with the project's name; done ones come back as history.
async def test_project_tasks_in_state(client, couple):  # noqa: F811
    pid, _ = planner.create_project(1, "Ремонт")
    a = planner.add_to_project(1, pid, "купить плитку до 30.10")[0][0]["id"]
    b = planner.add_to_project(1, pid, "вызвать мастера")[0][0]["id"]
    planner.done(1, b)
    state = await (await client.get("/api/state", headers=H1)).json()
    item, = [i for i in state["items"] if i["id"] == a]
    assert item["project"] == "Ремонт" and item["day"] == "2026-10-30"
    assert [i["id"] for i in state["done"] if i["kind"] == "task"] == [b]


# 9. A reminder at the due time goes once.
def test_reminder_at_time_once(couple):
    task(couple, "Позвонить в банк", day="2026-10-24", time="12:00")
    assert not reminders(NOW.replace(hour=11, minute=59))
    assert len(reminders(NOW.replace(hour=12))) >= 1
    assert not reminders(NOW.replace(hour=12, minute=1))


# 10. A reminder an hour before, and a day before (in the morning).
def test_reminder_before(couple):
    a = task(couple, "Позвонить в банк", day="2026-10-24", time="15:00")
    planner.edit(1, a, {"remind": 60})
    assert not reminders(NOW.replace(hour=13, minute=59))
    early = [o for o in reminders(NOW.replace(hour=14)) if "банк" in o.text]
    assert early and "заранее" in early[0].text and "15:00" in early[0].text
    b = task(couple, "Отвезти документы", day="2026-10-26")
    planner.edit(1, b, {"remind": 1440})
    assert [o for o in reminders(dt.datetime(2026, 10, 25, db.config.morning_hour)) if "документ" in o.text]


# 11. Done or deleted: no reminder.
def test_no_reminder_after_done_or_delete(couple):
    a = task(couple, "Позвонить в банк", day="2026-10-24", time="12:00")
    b = task(couple, "Позвонить маме", day="2026-10-24", time="12:00")
    planner.done(1, a)
    planner.remove(1, b)
    assert not reminders(NOW.replace(hour=13))


# 12. Moved: the old moment doesn't fire, the new one does, once.
def test_moved_task_reminds_at_new_time_only(couple):
    a = task(couple, "Позвонить в банк", day="2026-10-24", time="12:00")
    planner.edit(1, a, {"time": "18:00"})
    assert not reminders(NOW.replace(hour=12, minute=30))
    assert len(reminders(NOW.replace(hour=18))) >= 1
    assert not reminders(NOW.replace(hour=18, minute=5))


# 13. Reminder off.
def test_reminder_off(couple):
    a = task(couple, "Позвонить в банк", day="2026-10-24", time="12:00")
    planner.edit(1, a, {"remind": -1})
    assert not reminders(NOW.replace(hour=13))
    assert planner.edit(1, a, {"remind": 7})[1] == "Не понял, когда напомнить"


# 14. A daily task: each done day stays in history, the next appears, no duplicates on a second tap.
def test_daily_repeat_keeps_history(couple):
    a = task(couple, "Полить цветы", day="2026-10-24")
    assert planner.edit(1, a, {"repeat": "day"}) == (True, "")
    planner.done(1, a)
    planner.done(2, a)  # both partners at once
    nxt = db.series_open(couple, a)
    assert nxt["day"] == "2026-10-25" and db.item(a, couple)["status"] == "done"
    with closing(db._connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM items WHERE series_id = ?", (a,)).fetchone()[0] == 2
    planner.done(1, nxt["id"])
    assert db.series_open(couple, a)["day"] == "2026-10-26"
    assert len([r for r in db.done_items(couple, "task") if r["text"] == "Полить цветы"]) == 2


# 15. A weekly task; moving one instance doesn't move the series.
def test_weekly_repeat_move_one_instance(couple):
    a = task(couple, "Пропылесосить", day="2026-10-24")
    planner.edit(1, a, {"repeat": "week"})
    planner.edit(1, a, {"day": "2026-10-25"})  # this week on Sunday
    planner.done(1, a)
    assert db.series_open(couple, a)["day"] == "2026-10-31"


# 16. A monthly task; undo removes the untouched next instance.
def test_monthly_repeat_undo(couple):
    a = task(couple, "Оплатить кружок", day="2026-10-24")
    planner.edit(1, a, {"repeat": "month"})
    assert db.item(a, couple)["repeat"] == "month:24"
    planner.done(1, a)
    nxt = db.series_open(couple, a)
    assert nxt["day"] == "2026-11-24"
    assert planner.undo_done(1, a)
    assert db.item(nxt["id"], couple) is None and db.item(a, couple)["status"] == "open"
    planner.done(1, a)  # done again: one next instance, not two
    with closing(db._connect()) as c:
        assert c.execute("SELECT COUNT(*) FROM items WHERE series_id = ? AND status = 'open'", (a,)).fetchone()[0] == 1


# 17. Shopping: rename, bought, history, back, server dedupe.
async def test_shopping(client, couple):  # noqa: F811
    r = await (await client.post("/api/add", json={"text": "купить молоко"}, headers=H1)).json()
    iid = r["items"][0]["id"]
    again = await (await client.post("/api/add", json={"text": "Купить молоко"}, headers=H1)).json()
    assert again["items"][0]["existing"]
    assert (await client.patch(f"/api/items/{iid}", json={"text": "Молоко 3,2%"}, headers=H1)).status == 200
    await client.post(f"/api/items/{iid}/done", headers=H1)
    state = await (await client.get("/api/state", headers=H1)).json()
    assert [i["text"] for i in state["done"] if i["kind"] == "buy"] == ["Молоко 3,2%"]
    assert (await client.post(f"/api/items/{iid}/undo", headers=H1)).status == 200
    assert db.item(iid, couple)["status"] == "open"


# 18. Migration: an old base without the new columns keeps its rows.
def test_migration_keeps_old_rows(couple):
    iid = task(couple, "Надо вынести старый шкаф")
    with closing(db._connect()) as c:
        c.executescript("""
            DROP INDEX items_series;
            CREATE TABLE old AS SELECT id, couple_id, kind, text, author_id, assignee_id, day, time, end_time, repeat,
                project_id, weekday, status, created, done_at, reminded, nudges, last_nudge FROM items;
            DROP TABLE items; ALTER TABLE old RENAME TO items;
            CREATE TABLE p2 AS SELECT id, couple_id, name, about, author_id, created FROM projects;
            DROP TABLE projects; ALTER TABLE p2 RENAME TO projects;
            DROP TABLE project_sections;
        """)
    db.init()
    row = db.item(iid, couple)
    assert row["text"].endswith("старый шкаф") and row["note"] == "" and row["series_id"] is None
    assert planner.create_project(1, "Новый")[0]
