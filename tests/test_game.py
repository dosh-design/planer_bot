"""«Семья против бытового хаоса»: points, weekly matches, achievements, reminder settings, API and bot agreement."""
import datetime as dt
import sqlite3
import threading
from contextlib import closing
from types import SimpleNamespace

import pytest

import bot
import db
import game
import planner
from test_bot import FakeBot, message
from test_web import client, init_data  # noqa: F401  (client is a fixture)

MON = dt.date(2026, 10, 19)  # the week 19.10–25.10
WEEK = MON.isoformat()
NEXT = (MON + dt.timedelta(days=7)).isoformat()


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    t = {"now": dt.datetime(2026, 10, 20, 9, 0)}  # Tuesday
    monkeypatch.setattr(db, "local_now", lambda: t["now"])
    db.kv_set("game_start", "2026-10-01")  # the game is older than these weeks
    return t


def at(clock, day: dt.date, hour: int = 9):
    clock["now"] = dt.datetime.combine(day, dt.time(hour, 0))
    return clock["now"]


def task(couple, text="Ты обещал вкрутить лампочку", day: dt.date | None = None, uid=1) -> int:
    rows, _ = planner.add_text(uid, text, allow_model=False)
    if day:
        db.update_item(rows[0]["id"], couple, day=day.isoformat())
    return rows[0]["id"]


def score(couple, week=WEEK):
    s = db.game_score(couple, week)
    return s["family"], s["chaos"]


def events(couple):
    with closing(db._connect()) as c:
        return c.execute("SELECT side, item_id, instance, week, revoked FROM game_events WHERE couple_id = ?"
                         " ORDER BY id", (couple,)).fetchall()


# ---------- points ----------

def test_done_gives_the_family_one_point(couple):
    iid = task(couple)
    d = planner.complete(2, iid)
    assert d.ok and d.scored and score(couple) == (1, 0)
    assert d.toast.endswith("Неделя: 1:0")
    assert any("Семья 1 : 0 Бытовой хаос" in n.text for n in d.notes if n.chat_id == 1)


def test_second_tap_gives_nothing_more(couple):
    iid = task(couple)
    assert planner.complete(2, iid).scored
    again = planner.complete(2, iid)
    assert again.ok and not again.scored and not again.toast and not again.notes
    assert planner.complete(1, iid).notes == []  # the partner's late tap: nobody is told twice
    assert score(couple) == (1, 0) and len(events(couple)) == 1


def test_overdue_task_gives_chaos_one_point_however_long(couple, clock):
    task(couple, day=dt.date(2026, 10, 20))
    for d in (20, 21, 22, 23, 24, 25):
        planner.tick(at(clock, dt.date(2026, 10, d), 9))
    assert score(couple) == (0, 1)  # one point on the 21st, nothing for each further day
    planner.tick(at(clock, dt.date(2026, 10, 30), 9))
    assert score(couple) == (0, 1) and score(couple, NEXT) == (0, 0)


def test_doing_an_overdue_task_still_scores_and_unlocks_comeback(couple, clock):
    iid = task(couple, day=dt.date(2026, 10, 20))
    planner.tick(at(clock, dt.date(2026, 10, 21)))
    d = planner.complete(2, iid)
    assert d.scored and score(couple) == (1, 1)
    assert "comeback" in db.achievements(couple)
    assert [n.chat_id for n in d.notes if "Возвращение недели" in n.text] == [1, 2]  # to both, as a team


def test_undated_task_never_gives_chaos(couple, clock):
    task(couple)
    for d in range(20, 32):
        planner.tick(at(clock, dt.date(2026, 10, d)))
    assert score(couple)[1] == 0 and score(couple, NEXT)[1] == 0


def test_deleting_before_the_deadline_gives_nothing(couple, clock):
    iid = task(couple, day=dt.date(2026, 10, 22))
    assert planner.remove(1, iid)
    planner.tick(at(clock, dt.date(2026, 10, 24)))
    assert events(couple) == []


def test_moving_before_the_deadline_prevents_a_false_overdue(couple, clock):
    iid = task(couple, day=dt.date(2026, 10, 21))
    at(clock, dt.date(2026, 10, 21))
    assert planner.edit(2, iid, {"day": "2026-10-24"}) == (True, "")
    planner.tick(at(clock, dt.date(2026, 10, 22)))
    assert score(couple) == (0, 0)
    planner.tick(at(clock, dt.date(2026, 10, 25)))
    assert score(couple) == (0, 1)  # the new deadline passed: now it is the chaos's


def test_moving_an_overdue_task_keeps_the_chaos_point_and_gives_no_second(couple, clock):
    iid = task(couple, day=dt.date(2026, 10, 20))
    planner.tick(at(clock, dt.date(2026, 10, 21)))
    assert planner.edit(2, iid, {"day": "2026-10-23"}) == (True, "")
    assert score(couple) == (0, 1)  # history is not rewritten
    planner.tick(at(clock, dt.date(2026, 10, 27)))
    assert score(couple) == (0, 1) and score(couple, NEXT) == (0, 0)  # moving is not a way to dodge or repeat
    assert len(db.items(couple)) == 1  # no duplicate task


def test_deleting_after_the_deadline_keeps_the_chaos_point(couple, clock):
    iid = task(couple, day=dt.date(2026, 10, 20))
    planner.tick(at(clock, dt.date(2026, 10, 21)))
    planner.remove(1, iid)
    assert score(couple) == (0, 1)


def test_reprocessing_creates_no_duplicates(couple, clock):
    iid = task(couple, day=dt.date(2026, 10, 20))
    today = at(clock, dt.date(2026, 10, 21)).date()
    for _ in range(3):
        game.record_chaos(today)
        planner.tick(clock["now"])
    planner.complete(2, iid)
    planner.complete(2, iid)
    assert sorted(e["side"] for e in events(couple)) == ["chaos", "family"]


def test_undo_takes_the_point_back_and_redo_brings_the_same_one(couple):
    iid = task(couple)
    assert planner.complete(2, iid).scored
    assert planner.undo_done(2, iid) and score(couple) == (0, 0)
    d = planner.complete(2, iid)
    assert d.scored and score(couple) == (1, 0) and len(events(couple)) == 1
    assert not [n for n in d.notes if n.text.startswith("🏅")]  # «Первое дело» is not given again


def test_two_partners_at_once_score_one_point(couple):
    iid = task(couple)
    results, barrier = [], threading.Barrier(8)

    def tap(uid):
        barrier.wait()
        results.append(planner.complete(uid, iid))

    threads = [threading.Thread(target=tap, args=(1 + i % 2,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(r.ok for r in results) and sum(r.scored for r in results) == 1
    assert score(couple) == (1, 0)
    assert sum(1 for r in results for n in r.notes if n.text.startswith("✅")) == 1  # one «done» message, not eight


def test_couples_do_not_touch_each_other(couple):
    db.save_user(3, "Аня")
    db.save_user(4, "Петя")
    assert db.join(4, db.invite_code(3)) == "ok"
    other = db.user(3)["couple_id"]
    planner.complete(2, task(couple))
    assert score(couple) == (1, 0) and score(other) == (0, 0)
    assert not planner.complete(3, task(couple, "Ты обещал повесить полку")).ok  # someone else's item: not found
    assert score(couple) == (1, 0) and score(other) == (0, 0)


def test_monthly_payment_scores_each_month_once(couple, clock):
    rows, _ = planner.add_text(1, "21-го каждого месяца передать показания", allow_model=False)
    iid = rows[0]["id"]
    at(clock, dt.date(2026, 10, 21))
    assert planner.complete(1, iid).scored
    with closing(db._connect()) as c:
        fam = c.execute("SELECT instance FROM game_events WHERE side = 'family'").fetchall()
    assert [r["instance"] for r in fam][0] == "2026-10"
    planner.tick(at(clock, dt.date(2026, 12, 1)))  # November's was missed
    assert ("chaos", "2026-11") in [(e["side"], e["instance"]) for e in events(couple)]


def test_weekly_chore_scores_once_per_turn(couple, clock):
    rows, _ = planner.add_text(1, "По вторникам выносить мусор — на тебе", allow_model=False)
    iid = rows[0]["id"]
    at(clock, dt.date(2026, 10, 20))
    assert planner.complete(2, iid).scored
    assert not planner.complete(2, iid).scored
    at(clock, dt.date(2026, 10, 27))
    assert planner.complete(2, iid).scored
    assert score(couple) == (1, 0) and score(couple, NEXT) == (1, 0)
    planner.tick(at(clock, dt.date(2026, 11, 10)))
    assert not [e for e in events(couple) if e["side"] == "chaos"]  # chores never burn, so no chaos points


def test_purchases_and_plans_give_no_points(couple):
    buy, _ = planner.add_text(1, "купить молоко", allow_model=False)
    assert planner.complete(1, buy[0]["id"]).ok
    assert events(couple) == []


def test_tasks_overdue_before_the_game_started_are_not_counted(couple, clock):
    db.kv_set("game_start", "2026-10-22")
    task(couple, day=dt.date(2026, 10, 20))
    planner.tick(at(clock, dt.date(2026, 10, 23)))
    assert events(couple) == []


# ---------- weekly matches ----------

def _play(couple, clock, family: int, chaos: int, monday: dt.date = MON):
    """A real week: `family` tasks done, `chaos` tasks left past their day."""
    at(clock, monday)
    for i in range(chaos):
        task(couple, f"Ты обещал дело хаоса {monday} {i}", monday)
    for i in range(family):
        planner.complete(2, task(couple, f"Ты обещал дело семьи {monday} {i}"))
    game.record_chaos(monday + dt.timedelta(days=1))


def _results(out):
    return [o for o in out if o.text.startswith("<b>")]


def test_family_wins_the_week(couple, clock):
    _play(couple, clock, 3, 1)
    out = _results(planner.tick(at(clock, dt.date(2026, 10, 26), 10)))
    assert sorted(o.chat_id for o in out) == [1, 2]
    assert "Семья 3 : 1 Бытовой хаос" in out[0].text and "Новый матч" in out[0].text
    assert db.matches(couple)[0]["result"] == "family"


def test_chaos_wins_the_week_without_blame(couple, clock):
    _play(couple, clock, 1, 3)
    out = _results(planner.tick(at(clock, dt.date(2026, 10, 26), 10)))
    assert db.matches(couple)[0]["result"] == "chaos"
    text = out[0].text
    assert "Семья 1 : 3 Бытовой хаос" in text and "Маша" not in text and "Дима" not in text


def test_draw(couple, clock):
    _play(couple, clock, 2, 2)
    out = _results(planner.tick(at(clock, dt.date(2026, 10, 26), 10)))
    assert db.matches(couple)[0]["result"] == "draw" and "2 : 2" in out[0].text


def test_week_closes_once_and_a_new_match_starts(couple, clock):
    _play(couple, clock, 2, 0)
    now = at(clock, dt.date(2026, 10, 26), 10)
    assert _results(planner.tick(now))
    assert not _results(planner.tick(now + dt.timedelta(minutes=1)))  # the next minute of the loop
    assert not _results(planner.morning(now.date()))  # the morning job run again after a restart
    assert game.close_weeks(now.date()) == []
    assert score(couple, NEXT) == (0, 0)  # the new week starts from zero
    planner.complete(2, task(couple, "Ты обещал повесить полку"))
    assert score(couple, NEXT) == (1, 0) and score(couple) == (2, 0)  # the old week is history, untouched
    assert len(db.matches(couple)) == 1 and len(events(couple)) == 3


def test_points_at_the_week_boundary_go_to_the_right_match(couple, clock):
    sunday = dt.date(2026, 10, 25)
    iid = task(couple, day=sunday)
    at(clock, sunday, 23)
    planner.complete(2, task(couple, "Ты обещал повесить полку"))  # Sunday 23:00: this week
    clock["now"] = dt.datetime(2026, 10, 26, 0, 1)  # Monday 00:01
    planner.tick(clock["now"])
    assert score(couple) == (1, 1)  # Sunday's deadline belongs to Sunday's week
    planner.complete(2, iid)
    assert score(couple, NEXT) == (1, 0)  # done on Monday: the new week's point
    out = _results(planner.tick(at(clock, dt.date(2026, 10, 26), 10)))
    assert "Семья 1 : 1 Бытовой хаос" in out[0].text


def test_history_is_kept_week_after_week(couple, clock):
    _play(couple, clock, 2, 1)
    _play(couple, clock, 1, 2, MON + dt.timedelta(days=7))
    planner.tick(at(clock, dt.date(2026, 11, 2), 10))
    weeks = [(m["week"], m["family"], m["chaos"]) for m in db.matches(couple)]
    assert weeks == [(NEXT, 1, 2), (WEEK, 2, 1)]
    s = game.season(couple, clock["now"].date())
    assert s["last"]["week"] == NEXT and len(s["history"]) == 2


def test_a_failing_couple_does_not_stop_the_others(couple, clock, monkeypatch):
    _play(couple, clock, 2, 0)
    db.save_user(3, "Аня")
    db.save_user(4, "Петя")
    assert db.join(4, db.invite_code(3)) == "ok"
    other = db.user(3)["couple_id"]
    planner.complete(4, task(other, "Ты обещал погладить", uid=3))
    real = db.game_score

    def broken(cid, week):
        if cid == couple:
            raise RuntimeError("boom")
        return real(cid, week)

    monkeypatch.setattr(db, "game_score", broken)
    out = game.close_weeks(dt.date(2026, 10, 26))
    assert [cid for cid, _ in out] == [other]
    monkeypatch.setattr(db, "game_score", real)
    assert [cid for cid, _ in game.close_weeks(dt.date(2026, 10, 26))] == [couple]  # retried later, not lost


# ---------- achievements ----------

def test_first_done_is_given_once(couple):
    d = planner.complete(2, task(couple))
    assert [n.chat_id for n in d.notes if "«Первое дело»" in n.text] == [1, 2]
    d = planner.complete(2, task(couple, "Ты обещал повесить полку"))
    assert not [n for n in d.notes if n.text.startswith("🏅")]
    assert list(db.achievements(couple)) == ["first_done"]


def test_long_haul_counts_real_dates(couple, clock):
    iid = task(couple)
    old = task(couple, "Ты обещал повесить полку")  # both written down on 20.10
    at(clock, dt.date(2026, 10, 20) + dt.timedelta(days=game.LONG_HAUL_DAYS - 1))
    planner.complete(2, iid)
    assert "long_haul" not in db.achievements(couple)
    at(clock, dt.date(2026, 10, 20) + dt.timedelta(days=30))
    d = planner.complete(2, old)
    assert "long_haul" in db.achievements(couple)
    assert any("ждало 30 дней" in n.text for n in d.notes)


def test_clean_sheet_only_without_real_overdue(couple, clock):
    _play(couple, clock, 2, 1)
    planner.tick(at(clock, dt.date(2026, 10, 26), 10))
    assert "clean_sheet" not in db.achievements(couple)
    _play(couple, clock, 2, 0, MON + dt.timedelta(days=7))
    out = _results(planner.tick(at(clock, dt.date(2026, 11, 2), 10)))
    assert "clean_sheet" in db.achievements(couple) and "Чистый лист" in out[0].text


def test_clean_sheet_needs_a_whole_week_watched_by_the_game(couple, clock):
    db.kv_set("game_start", "2026-10-21")  # the game appeared on Wednesday of that week
    _play(couple, clock, 2, 0)
    planner.tick(at(clock, dt.date(2026, 10, 26), 10))
    assert "clean_sheet" not in db.achievements(couple)


def test_rout_threshold(couple, clock):
    _play(couple, clock, game.ROUT_MARGIN, 1)  # won by one point less than the margin
    planner.tick(at(clock, dt.date(2026, 10, 26), 10))
    assert "rout" not in db.achievements(couple)
    _play(couple, clock, game.ROUT_MARGIN, 0, MON + dt.timedelta(days=7))
    planner.tick(at(clock, dt.date(2026, 11, 2), 10))
    assert "rout" in db.achievements(couple)


def test_streak_counts_weekly_wins_back_to_back(couple, clock):
    for w in range(game.STREAK_WEEKS - 1):
        _play(couple, clock, 2, 0, MON + dt.timedelta(days=7 * w))
    planner.tick(at(clock, MON + dt.timedelta(days=7 * (game.STREAK_WEEKS - 1)), 10))
    assert "streak" not in db.achievements(couple)
    _play(couple, clock, 2, 0, MON + dt.timedelta(days=7 * (game.STREAK_WEEKS - 1)))
    planner.tick(at(clock, MON + dt.timedelta(days=7 * game.STREAK_WEEKS), 10))
    assert "streak" in db.achievements(couple)


def test_streak_is_not_made_of_tasks_or_broken_weeks(couple, clock):
    _play(couple, clock, 12, 0)  # a lot of tasks in one week is one win, not a series
    _play(couple, clock, 2, 0, MON + dt.timedelta(days=14))  # a week without a match in between
    _play(couple, clock, 2, 0, MON + dt.timedelta(days=21))
    planner.tick(at(clock, MON + dt.timedelta(days=28), 10))
    assert len(db.matches(couple)) == 3 and "streak" not in db.achievements(couple)


def test_client_cannot_write_points(couple):
    buy, _ = planner.add_text(1, "купить молоко", allow_model=False)
    planner.complete(1, buy[0]["id"])
    assert not planner.complete(1, 9999).ok
    assert events(couple) == [] and db.achievements(couple) == {}


# ---------- reminders: settings and failures ----------

def _on(clock, n, due=dt.date(2026, 10, 20)):
    return at(clock, due + dt.timedelta(days=n), 10)


def test_reminder_modes(couple, clock):
    task(couple, day=dt.date(2026, 10, 20))
    texts = {}
    for mode in ("humor", "soft", "min", "off"):
        db.set_nudge_mode(2, mode)
        with closing(db._connect()) as c:
            c.execute("UPDATE items SET nudges = 0, last_nudge = NULL")
        db.kv_set("morning", "")
        texts[mode] = {}
        for n in range(0, 15):
            db.kv_set("morning", "")
            got = [o.text for o in planner.morning(_on(clock, n).date()) if "лампочк" in o.text.lower()
                   and not o.text.startswith("☀️")]
            if got:
                texts[mode][n] = got[0]
    assert sorted(texts["humor"]) == [2, 4, 7, 14] and texts["humor"][4].startswith("😏")
    assert sorted(texts["soft"]) == [2, 4, 7, 14]
    assert not any("😏" in t or "Пацан" in t for t in texts["soft"].values())
    assert sorted(texts["min"]) == [2, 7]
    assert texts["off"] == {}


def test_off_still_counts_the_stage_so_turning_on_does_not_flood(couple, clock):
    task(couple, day=dt.date(2026, 10, 20))
    db.set_nudge_mode(2, "off")
    for n in range(0, 8):
        planner.tick(_on(clock, n))
    db.set_nudge_mode(2, "humor")
    assert not [o for o in planner.tick(_on(clock, 8)) if "лампочк" in o.text.lower()]


def test_deadline_day_line_in_the_digest(couple, clock):
    task(couple, day=dt.date(2026, 10, 22))
    out = {o.chat_id: o.text for o in planner.morning(dt.date(2026, 10, 22))}
    assert any(line in out[2] for line in ("важный день", "Дедлайн на горизонте", "присматривается"))
    db.set_nudge_mode(2, "off")
    out = {o.chat_id: o.text for o in planner.morning(dt.date(2026, 10, 22))}
    assert "лампочку" in out[2] and not any(w in out[2] for w in ("важный день", "горизонте", "присматривается"))


def test_failed_send_is_not_retried_forever(couple, clock):
    task(couple, day=dt.date(2026, 10, 20))
    lost = [o for o in planner.tick(_on(clock, 2)) if "лампочк" in o.text.lower()]
    assert lost  # suppose Telegram dropped it: bot.send gives up after two tries (test_bot)
    for minute in range(1, 5):
        assert not [o for o in planner.tick(_on(clock, 2) + dt.timedelta(minutes=minute)) if "лампочк" in o.text.lower()]
    assert not [o for o in planner.tick(_on(clock, 3)) if "лампочк" in o.text.lower()]


# ---------- bot, mini-app and data ----------

async def test_api_done_reports_the_server_score_once(client, couple):  # noqa: F811
    h = {"X-Init-Data": init_data(2, "Дима")}
    iid = task(couple)
    r = await (await client.post(f"/api/items/{iid}/done", headers=h)).json()
    assert r["scored"] and r["toast"] and (r["score"]["family"], r["score"]["chaos"]) == (1, 0)
    r = await (await client.post(f"/api/items/{iid}/done", headers=h)).json()  # a retried request
    assert not r["scored"] and not r["toast"] and r["score"]["family"] == 1
    state = await (await client.get("/api/state", headers=h)).json()
    assert state["score"]["family"] == 1 and state["score"]["status"] == "На этой неделе вы впереди!"


async def test_bot_and_app_show_the_same_score(client, couple):  # noqa: F811
    planner.complete(2, task(couple))
    season = await (await client.get("/api/season", headers={"X-Init-Data": init_data(1, "Маша")})).json()
    m = message(1, "Маша", "/season")
    await bot.season(m)
    text = m.answers[0][0]
    cur = season["current"]
    assert f"Семья {cur['family']} : {cur['chaos']} Бытовой хаос" in text
    assert [a["code"] for a in season["achievements"] if a["unlocked"]] == ["first_done"]
    assert "Первое дело" in text and len(season["achievements"]) == len(game.ACHIEVEMENTS)


async def test_bot_done_button_shows_the_score(couple):
    iid = task(couple)
    answers, edits = [], []

    async def answer(text=None, **kw):
        answers.append(text)

    async def edit_text(text, **kw):
        edits.append(text)

    msg = SimpleNamespace(html_text="📌 Вкрутить лампочку", edit_text=edit_text)
    cb = SimpleNamespace(from_user=SimpleNamespace(id=2), data=f"done:{iid}", answer=answer, message=msg)
    await bot.cb_done(cb, FakeBot())
    assert "Неделя: 1:0" in answers[0] and "✅ Сделано · " in edits[0]
    await bot.cb_done(cb, FakeBot())
    assert answers[1] == "Готово!" and score(couple) == (1, 0)


async def test_forged_requests_cannot_touch_another_couples_score(client, couple):  # noqa: F811
    db.save_user(3, "Аня")
    iid = task(couple)
    assert (await client.post(f"/api/items/{iid}/done")).status == 401  # no Telegram signature
    r = await client.post(f"/api/items/{iid}/done", headers={"X-Init-Data": init_data(3, "Аня")})
    assert r.status == 404 and score(couple) == (0, 0)
    season = await (await client.get("/api/season", headers={"X-Init-Data": init_data(3, "Аня")})).json()
    assert season["current"]["family"] == 0 and season["history"] == []
    assert (await client.post("/api/season", headers={"X-Init-Data": init_data(3, "Аня")})).status == 405


async def test_settings_api(client, couple):  # noqa: F811
    h = {"X-Init-Data": init_data(2, "Дима")}
    assert (await client.post("/api/settings", json={"nudge_mode": "loud"}, headers=h)).status == 400
    r = await client.post("/api/settings", json={"nudge_mode": "soft"}, headers=h)
    assert r.status == 200 and planner.nudge_mode(2) == "soft" and planner.nudge_mode(1) == "humor"
    season = await (await client.get("/api/season", headers=h)).json()
    assert season["nudge_mode"] == "soft"


async def test_bot_reminders_setting(couple):
    m = message(2, "Дима", "/reminders")
    await bot.reminders(m)
    assert "Мягко" in m.answers[0][0]
    answers = []

    async def answer(text=None, **kw):
        answers.append(text)

    cb = SimpleNamespace(from_user=SimpleNamespace(id=2), data="nmode:min", answer=answer, message=None)
    await bot.cb_mode(cb)
    assert planner.nudge_mode(2) == "min" and "Минимум" in answers[0]


def test_solo_points_join_the_couple(clock):
    db.save_user(1, "Маша")
    db.save_user(2, "Дима")
    planner.complete(2, _solo_task(2))  # Дима starts alone
    solo = db.user(2)["couple_id"]
    assert score(solo) == (1, 0)
    assert db.join(2, db.invite_code(1)) == "ok"
    cid = db.user(1)["couple_id"]
    assert score(cid) == (1, 0) and "first_done" in db.achievements(cid)


def _solo_task(uid):
    rows, _ = planner.add_text(uid, "повесить полку", allow_model=False)
    return rows[0]["id"]


def test_deleting_the_last_person_removes_the_score(couple):
    planner.complete(2, task(couple))
    db.delete_user(1)
    db.delete_user(2)
    assert events(couple) == [] and db.matches(couple) == [] and db.achievements(couple) == {}


def test_old_base_gets_the_settings_column(tmp_path):
    path = db.config.data_dir / "family.sqlite3"
    path.unlink()
    with closing(sqlite3.connect(path)) as c:
        c.executescript("""CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT NOT NULL, couple_id INTEGER, created TEXT NOT NULL);
            INSERT INTO users VALUES (5, 'Старый', NULL, '2026-10-01 10:00');""")
        c.commit()
    db.init()
    db.init()
    assert db.user(5)["nudge_mode"] == "humor" and planner.nudge_mode(5) == "humor"
