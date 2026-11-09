"""Family achievements with three tiers: bronze, silver, gold by how many times the couple earned them."""
import datetime as dt
from contextlib import closing

import pytest

import db
import game
import planner

TUE = dt.date(2026, 10, 20)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    t = {"now": dt.datetime.combine(TUE, dt.time(12, 0))}
    monkeypatch.setattr(db, "local_now", lambda: t["now"])
    db.kv_set("game_start", "2026-10-01")
    return t


def at(clock, day: dt.date, hour: int = 12, minute: int = 0):
    clock["now"] = dt.datetime.combine(day, dt.time(hour, minute))


def task(text="Повесить полку", uid=1) -> int:
    rows, _ = planner.add_text(uid, f"Ты обещал сделать {text}", allow_model=False)
    return rows[0]["id"]


def done(text="Повесить полку") -> list[str]:
    return [n.text for n in planner.complete(2, task(text)).notes if n.chat_id == 1]


def ach(couple) -> dict:
    return {a["code"]: a for a in game.achievement_list(couple)}


def test_every_achievement_has_three_growing_tiers():
    for code, (icon, title, about, needs, forms) in game.ACHIEVEMENTS.items():
        assert len(needs) == 3 and needs[0] < needs[1] < needs[2], code
        assert len(forms) == 3 and title and about and icon


@pytest.mark.parametrize("n, text", [(1, "1 дело"), (2, "2 дела"), (5, "5 дел"), (11, "11 дел"), (21, "21 дело"),
                                     (22, "22 дела"), (112, "112 дел")])
def test_plural(n, text):
    assert game.plural(n, game.DEEDS) == text


def test_done_together_climbs_from_bronze_to_silver(couple):
    first = done("Дело 0")
    assert any(t.startswith("🥉 Бронза: «Сделано вместе»") and "серебро за 50 дел" in t for t in first)
    silver_at = None
    for i in range(1, 50):
        if any("🥈 Серебро: «Сделано вместе»" in t for t in done(f"Дело {i}")):
            silver_at = i + 1
    assert silver_at == 50
    a = ach(couple)["done_total"]
    assert (a["tier"], a["count"], a["next"]["tier"], a["next"]["need"]) == ("silver", 50, "gold", 200)


def test_a_tier_is_announced_once(couple):
    done("Первое")
    assert not any("Бронза" in t for t in done("Второе"))
    assert sorted(k for k in db.achievements(couple)) == ["done_total:bronze"]


def test_busy_day_needs_five_tasks_in_one_day(couple, clock):
    for i in range(game.BUSY_DAY - 1):
        done(f"Дело {i}")
    assert ach(couple)["busy_day"]["count"] == 0
    assert any("«Продуктивный день»" in t for t in done("Пятое"))
    done("Шестое")  # the same day counts once
    assert ach(couple)["busy_day"]["count"] == 1
    at(clock, TUE + dt.timedelta(days=1))
    for i in range(game.BUSY_DAY):
        done(f"Завтра {i}")
    assert ach(couple)["busy_day"]["count"] == 2


def test_early_birds_and_night_owls(couple, clock):
    at(clock, TUE, 7, 40)
    assert any("«Ранние пташки»" in t and "07:40" in t for t in done("Вынести мусор"))
    at(clock, TUE, 12)
    done("Днём")
    at(clock, TUE, 23, 15)
    assert any("«Ночная смена»" in t for t in done("Помыть посуду"))
    a = ach(couple)
    assert (a["early_birds"]["count"], a["night_owls"]["count"]) == (1, 1)


def test_bills_only_for_a_payment_in_time(couple, clock):
    at(clock, dt.date(2026, 11, 3))
    rows, _ = planner.add_text(1, "оплатить интернет 650 ₽ каждый месяц 10 числа", allow_model=False)
    notes = [n.text for n in planner.complete(1, rows[0]["id"]).notes]
    assert ach(couple)["bills"]["count"] == 1 and any("«Без пени»" in t for t in notes)
    late, _ = planner.add_text(1, "оплатить свет каждый месяц 1 числа", allow_model=False)
    db.update_item(late[0]["id"], couple, day="2026-11-01")
    planner.complete(1, late[0]["id"])  # two days late: a family point, but no «Без пени»
    assert ach(couple)["bills"]["count"] == 1


def test_project_done_when_the_last_step_is_closed(couple):
    pid, _ = planner.create_project(1, "Ремонт кухни")
    a, _, _ = planner.add_to_project(1, pid, "купить плитку")
    b, _, _ = planner.add_to_project(1, pid, "вызвать мастера")
    planner.complete(2, a[0]["id"])
    assert ach(couple)["project_done"]["count"] == 0
    notes = [n.text for n in planner.complete(2, b[0]["id"]).notes]
    assert any("«Большое дело»" in t and "Ремонт кухни" in t for t in notes)


def test_every_day_of_the_week(couple, clock):
    mon = db.week_of(TUE)
    for d in range(7):
        at(clock, mon + dt.timedelta(days=d))
        done(f"День {d}")
    assert ach(couple)["every_day"]["count"] == 1
    assert ach(couple)["weekend"]["count"] == 0  # one task on Saturday and one on Sunday is not «с пользой»


def test_weekend_with_four_tasks(couple, clock):
    sat = db.week_of(TUE) + dt.timedelta(days=5)
    at(clock, sat)
    done("Субботнее 1")
    done("Субботнее 2")
    at(clock, sat + dt.timedelta(days=1))
    done("Воскресное 1")
    assert any("«Выходные с пользой»" in t for t in done("Воскресное 2"))


def test_season_shows_all_tiers_and_what_is_next(couple):
    done()
    lst = game.achievement_list(couple)
    assert len(lst) == len(game.ACHIEVEMENTS)
    assert all([t["tier"] for t in a["tiers"]] == list(game.TIERS) for a in lst)
    night = next(a for a in lst if a["code"] == "night_owls")
    assert night["tier"] is None and night["next"]["need_text"] == "1 дело"
    streak = next(a for a in lst if a["code"] == "streak")
    assert streak["next"]["need_text"] == "3 недели подряд"


def test_old_one_time_achievements_become_bronze(couple):
    with closing(db._connect()) as c:
        for code in ("first_done", "rout", "streak", "comeback"):
            c.execute("INSERT INTO game_achievements (couple_id, code, unlocked, detail) VALUES (?, ?, '2026-10-01 10:00', '')",
                      (couple, code))
        c.execute("INSERT INTO game_matches (couple_id, week, family, chaos, result, closed)"
                  " VALUES (?, '2026-09-28', 16, 0, 'family', '2026-10-05 10:00')", (couple,))
    db.init()
    db.init()  # twice: nothing doubles
    got = db.achievements(couple)
    assert {"done_total:bronze", "rout:bronze", "streak:bronze", "comeback:bronze"} <= set(got)
    a = ach(couple)
    assert (a["streak"]["count"], a["comeback"]["count"], a["clean_sheet"]["count"], a["big_week"]["count"]) == (3, 1, 1, 1)
    assert a["rout"]["count"] == 1 and a["rout"]["tier"] == "bronze"


def test_points_and_medals_follow_the_couple_when_a_solo_player_joins():
    db.save_user(1, "Маша")
    db.save_user(2, "Дима")
    rows, _ = planner.add_text(2, "повесить полку", allow_model=False)
    planner.complete(2, rows[0]["id"])
    at_night = db.user(2)["couple_id"]
    db.add_feat(at_night, "night_owls", "x")
    assert db.join(2, db.invite_code(1)) == "ok"
    cid = db.user(1)["couple_id"]
    assert db.feat_counts(cid).get("night_owls") == 1
