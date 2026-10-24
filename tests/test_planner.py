import datetime as dt
from contextlib import closing

import pytest

import db
import planner

NOW = dt.datetime(2026, 10, 22, 9, 0)  # Thursday morning


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    t = {"now": NOW}
    monkeypatch.setattr(db, "local_now", lambda: t["now"])
    return t


def test_join_rules(couple):
    code = db.couple_code(1)
    assert db.join(2, code) == "self"
    db.save_user(3, "Третий")
    assert db.join(3, code) == "full"
    assert db.join(3, "nope") == "unknown"
    assert db.partner(1)["name"] == "Дима"


def test_items_made_alone_move_to_the_couple():
    db.save_user(1, "Маша")
    db.save_user(2, "Дима")
    planner.add_text(2, "купить пиво")  # Дима starts alone
    assert db.join(2, db.couple_code(1)) == "ok"
    assert [r["text"] for r in db.items(db.user(1)["couple_id"], "buy")] == ["Пиво"]


def test_task_for_partner_notifies_partner(couple):
    rows, notes = planner.add_text(1, "Ты обещал вкрутить лампочку до субботы")
    assert rows[0]["assignee_id"] == 2 and rows[0]["day"] == "2026-10-24"
    assert len(notes) == 1 and notes[0].chat_id == 2
    assert "Маша записал(а) на тебя" in notes[0].text and "лампочку" in notes[0].text
    assert notes[0].buttons[0][0][1] == f"done:{rows[0]['id']}"


def test_shopping_goes_to_partner_as_one_message(couple):
    rows, notes = planner.add_text(2, "Купить хлеб, молоко")
    assert len(rows) == 2 and [n.chat_id for n in notes] == [1]
    assert "Хлеб, Молоко" in notes[0].text


def test_joint_plan_on_busy_day_warns(couple):
    planner.add_text(2, "По субботам у меня баня")
    rows, _ = planner.add_text(1, "В субботу едем к маме")
    warn = planner.warnings(rows[0])
    assert len(warn) == 1 and "Дима занят(а)" in warn[0] and "баня" in warn[0].lower()


def test_busy_day_on_top_of_joint_plan_warns(couple):
    planner.add_text(1, "В субботу едем к маме")
    rows, _ = planner.add_text(2, "В субботу у меня футбол")
    assert "общий план" in planner.warnings(rows[0])[0]


def test_done_tells_partner(couple):
    rows, _ = planner.add_text(1, "Ты обещал вкрутить лампочку")
    ok, notes = planner.done(2, rows[0]["id"])
    assert ok and notes[0].chat_id == 1 and "Дима" in notes[0].text
    assert planner.done(2, rows[0]["id"])[0] is True  # ticking twice is harmless
    assert not db.items(couple, "task")


def test_strangers_cannot_touch_items(couple):
    rows, _ = planner.add_text(1, "Ты обещал вкрутить лампочку")
    db.save_user(9, "Чужой")
    db.couple_code(9)
    assert planner.done(9, rows[0]["id"]) == (False, [])
    assert not planner.reassign(9, rows[0]["id"], "me")


def test_exact_time_reminder_once(couple, clock):
    rows, _ = planner.add_text(1, "Ты обещал сегодня в 18:00 забрать посылку")
    assert planner.due(NOW) == []
    clock["now"] = NOW.replace(hour=18)
    out = planner.due(clock["now"])
    assert [o.chat_id for o in out] == [2] and "посылку" in out[0].text
    assert planner.due(clock["now"]) == []


def test_joint_reminder_goes_to_both(couple):
    planner.add_text(1, "сегодня в 20:00 вместе смотрим кино")
    out = planner.due(NOW.replace(hour=20))
    assert sorted(o.chat_id for o in out) == [1, 2]


def _age(item_id: int, days: int) -> None:
    created = (NOW - dt.timedelta(days=days)).strftime("%Y-%m-%d %H:%M")
    with closing(db._connect()) as c:
        c.execute("UPDATE items SET created = ? WHERE id = ?", (created, item_id))


def test_old_promise_gets_sarcastic_nudge_then_rests(couple):
    rows, _ = planner.add_text(1, "Ты обещал вкрутить лампочку")
    _age(rows[0]["id"], 14)
    out = planner.morning(NOW.date())
    nudges = [o for o in out if "лампочк" in o.text.lower()]
    assert [o.chat_id for o in nudges] == [2] and "14" in nudges[0].text
    assert "Ты обещал" not in nudges[0].text
    assert not [o for o in planner.morning(NOW.date()) if "лампочк" in o.text.lower()]
    later = NOW.date() + dt.timedelta(days=3)
    assert [o for o in planner.morning(later) if "лампочк" in o.text.lower()]


def test_undated_task_shows_how_long_it_hangs(couple):
    rows, _ = planner.add_text(1, "Ты обещал вкрутить лампочку")
    assert planner.when_text(rows[0], NOW.date()) == ""
    _age(rows[0]["id"], 14)
    assert planner.when_text(db.item(rows[0]["id"], couple), NOW.date()) == "висит 14 дней"


def test_fresh_promise_is_not_nudged(couple):
    planner.add_text(1, "Ты обещал вкрутить лампочку")
    assert not planner.morning(NOW.date())


def test_morning_digest_mentions_partner_busy_day(couple):
    planner.add_text(2, "По четвергам у меня футбол")
    planner.add_text(1, "сегодня забрать кота от ветеринара")
    out = {o.chat_id: o.text for o in planner.morning(NOW.date())}
    assert "Дима сегодня занят(а): Футбол" in out[1] and "кота" in out[1]
    assert 2 not in out  # his own football is not news to him


def test_tick_runs_morning_once_a_day(couple, clock):
    rows, _ = planner.add_text(1, "Ты обещал вкрутить лампочку")
    _age(rows[0]["id"], 5)
    assert not planner.tick(NOW)  # 09:00 is before MORNING_HOUR=10
    clock["now"] = NOW.replace(hour=10)
    assert planner.tick(clock["now"])
    assert not planner.tick(clock["now"].replace(minute=1))


def test_delete_user_keeps_shared_items_for_partner(couple):
    planner.add_text(1, "Ты обещал вкрутить лампочку")
    planner.add_text(2, "я помою окна")
    db.delete_user(2)
    rows = db.items(couple, "task")
    assert len(rows) == 2 and all(r["assignee_id"] is None for r in rows)
    db.delete_user(1)
    assert not db.items(couple)


def test_overdue_and_long_hanging_tasks_burn(couple):
    today = NOW.date()
    late = db.item(db.add_item(couple, 1, "task", "Оплатить интернет", 1, "2026-10-20"), couple)
    assert planner.is_hot(late, today) and planner.when_text(late, today) == "просрочено на 2 дня"
    assert planner.line(late, today).startswith("🔥")
    yesterday = db.item(db.add_item(couple, 1, "event", "Кино", None, "2026-10-21"), couple)
    assert planner.when_text(yesterday, today) == "просрочено со вчера"
    fresh = db.item(db.add_item(couple, 1, "task", "Позвонить маме", 1, "2026-10-22"), couple)
    assert not planner.is_hot(fresh, today)
    rows, _ = planner.add_text(1, "Ты обещал вкрутить лампочку")
    _age(rows[0]["id"], 6)
    assert not planner.is_hot(db.item(rows[0]["id"], couple), today)
    _age(rows[0]["id"], 7)
    assert planner.is_hot(db.item(rows[0]["id"], couple), today)
    busy = db.item(db.add_item(couple, 2, "busy", "Баня", 2, "2026-10-18"), couple)
    assert not planner.is_hot(busy, today)  # someone's past football is not a fire
