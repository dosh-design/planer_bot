"""Staged overdue nudges: day 2 soft, day 4 sarcasm, day 7 the motto, then every doubling; no duplicates, no spam."""
import datetime as dt

import pytest

import db
import planner

DUE = dt.date(2026, 10, 20)
MOTTO = "Пацан сказал — пацан сделал!"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    t = {"now": dt.datetime(2026, 10, 20, 9, 0)}
    monkeypatch.setattr(db, "local_now", lambda: t["now"])
    return t


def _task(couple, text="Ты обещал вкрутить лампочку", day=DUE):
    rows, _ = planner.add_text(1, text, allow_model=False)
    db.update_item(rows[0]["id"], couple, day=day.isoformat())
    return rows[0]["id"]


def _on(day_n: int) -> dt.datetime:
    return dt.datetime.combine(DUE + dt.timedelta(days=day_n), dt.time(db.config.morning_hour, 0))


def _nudges(out, word="лампочк"):
    return [o for o in out if word in o.text.lower() and not o.text.startswith("☀️")]


@pytest.mark.parametrize("days, stage", [
    (-3, 0), (0, 0), (1, 0), (2, 1), (3, 1), (4, 2), (6, 2), (7, 3), (13, 3), (14, 4), (27, 4), (28, 5), (56, 6), (111, 6), (112, 7),
])
def test_stage_by_days_overdue(days, stage):
    assert planner.overdue_stage(days) == stage


def test_two_months_of_mornings_follow_the_schedule(couple):
    _task(couple)
    sent = {}
    for n in range(0, 61):
        for o in _nudges(planner.tick(_on(n))):
            sent.setdefault(n, []).append(o)
    assert sorted(sent) == [2, 4, 7, 14, 28, 56]  # increasing gaps after the week, never daily
    assert all([o.chat_id for o in v] == [2] for v in sent.values())  # only the one who has to do it
    assert sent[2][0].text.startswith("🙂") and MOTTO not in sent[2][0].text
    assert sent[4][0].text.startswith("😏")
    assert MOTTO in sent[7][0].text
    assert "28 дней" in sent[28][0].text


def test_rerun_and_restart_send_nothing_twice(couple):
    _task(couple)
    first = _nudges(planner.tick(_on(2)))
    assert len(first) == 1
    assert not _nudges(planner.tick(_on(2) + dt.timedelta(minutes=1)))  # the next minute of the loop
    assert not _nudges(planner.morning(_on(2).date()))  # the morning job run again (e.g. after a restart)
    db.kv_set("morning", "")  # the day mark lost: still no duplicate, the stage is stored on the task
    assert not _nudges(planner.tick(_on(2)))
    assert not _nudges(planner.tick(_on(3)))


def test_after_downtime_only_the_latest_stage_goes(couple):
    _task(couple)
    out = _nudges(planner.tick(_on(10)))  # the bot was down from day 1 to day 10
    assert len(out) == 1 and MOTTO in out[0].text
    assert not any(_nudges(planner.tick(_on(n))) for n in (11, 12, 13))
    assert _nudges(planner.tick(_on(14)))


def test_moved_deadline_starts_over_from_the_new_date(couple):
    iid = _task(couple)
    assert _nudges(planner.tick(_on(4)))  # sarcasm already went
    new = DUE + dt.timedelta(days=6)  # on day 4 moved to two days ahead
    assert planner.edit(2, iid, {"day": new.isoformat()}) == (True, "")
    for n in (5, 6, 7):
        assert not _nudges(planner.tick(_on(n))), n  # the old schedule (day 7 motto) does not fire
    out = _nudges(planner.tick(_on(8)))  # the new date + 2 days
    assert len(out) == 1 and out[0].text.startswith("🙂")


def test_moved_to_another_past_date_restarts_too(couple):
    iid = _task(couple)
    assert _nudges(planner.tick(_on(7)))
    planner.edit(2, iid, {"day": (_on(8).date() - dt.timedelta(days=2)).isoformat()})
    out = _nudges(planner.tick(_on(8)))
    assert len(out) == 1 and out[0].text.startswith("🙂")


def test_editing_other_fields_keeps_the_schedule(couple):
    iid = _task(couple)
    assert _nudges(planner.tick(_on(2)))
    planner.edit(2, iid, {"text": "Вкрутить лампочку в ванной", "day": DUE.isoformat()})  # same date sent again
    assert not _nudges(planner.tick(_on(3)))
    assert _nudges(planner.tick(_on(4)))


def test_done_cancelled_or_deleted_task_gets_nothing(couple):
    done = _task(couple, "Ты обещал вкрутить лампочку")
    gone = _task(couple, "Ты обещал повесить полку")
    out = planner.tick(_on(2))
    assert _nudges(out) and _nudges(out, "полк")
    planner.done(2, done)
    assert planner.remove(1, gone)
    for n in range(3, 30):
        out = planner.tick(_on(n))
        assert not _nudges(out) and not _nudges(out, "полк"), n


def test_undone_task_continues_from_its_stage(couple):
    iid = _task(couple)
    assert _nudges(planner.tick(_on(2)))
    planner.done(2, iid)
    assert not _nudges(planner.tick(_on(4)))
    planner.undo_done(2, iid)
    assert MOTTO in _nudges(planner.tick(_on(7)))[0].text


def test_events_chores_and_undated_project_steps_are_not_nudged(couple):
    planner.add_text(1, "20.10 ужин у мамы", allow_model=False)
    planner.add_text(1, "По вторникам выносить мусор — на тебе", allow_model=False)
    pid, _ = planner.create_project(1, "Ремонт")
    planner.add_to_project(1, pid, "выбрать плитку")
    for n in range(0, 15):
        out = planner.tick(_on(n))
        assert not [o for o in out if not o.text.startswith("☀️")], (n, [o.text for o in out])


def test_many_overdue_tasks_are_capped_per_person(couple):
    words = ["лампочк", "полк", "кран", "розетк", "дверц"]
    for text in ("вкрутить лампочку", "повесить полку", "починить кран", "поменять розетку", "подтянуть дверцу"):
        _task(couple, f"Ты обещал {text}")
    out = planner.tick(_on(2))
    to_dima = [o for o in out if o.chat_id == 2]
    nudged = [w for w in words if any(w in o.text.lower() for o in to_dima)]
    assert len(nudged) == 3
    queue = [o for o in to_dima if o.text.startswith("⌛ В очереди")]
    assert len(queue) == 1 and "ещё 2 просроченных дела" in queue[0].text
    assert not [o for o in out if o.chat_id == 1]  # Маша is not spammed with Дима's tasks
    nxt = [o for o in planner.tick(_on(3)) if o.chat_id == 2]
    rest = [w for w in words if any(w in o.text.lower() for o in nxt)]
    assert sorted(rest) == sorted(set(words) - set(nudged))  # the waiting ones come the next morning
    assert not [o for o in nxt if o.text.startswith("⌛ В очереди")]


def test_cap_puts_the_longest_overdue_first(couple):
    for i, text in enumerate(("вкрутить лампочку", "повесить полку", "починить кран", "поменять розетку")):
        _task(couple, f"Ты обещал {text}", DUE - dt.timedelta(days=i))
    out = [o.text.lower() for o in planner.tick(_on(2)) if o.chat_id == 2 and not o.text.startswith("⌛")]
    assert len(out) == 3 and not any("лампочк" in t for t in out)  # the freshest one waits


def test_undated_promise_keeps_the_old_every_three_days_nudge(couple):
    planner.add_text(1, "Ты обещал вкрутить лампочку", allow_model=False)
    days = [n for n in range(0, 10) if _nudges(planner.tick(_on(n)))]
    assert days == [3, 6, 9]
