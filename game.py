"""«Семья против бытового хаоса»: the couple's shared weekly score. Partners are one team, never rivals:
nothing here counts or shows who of the two did what.

    family +1  a task really done (once per task; once per turn of a weekly chore; once per month of a payment)
    chaos  +1  a dated task still open after its day (once per task, however long it stays overdue)

A point is a row in game_events; the database's unique key, not the caller, keeps it from being counted twice.
Points come only from real changes of real items on the server: no API writes them.
A match is a calendar week, Monday 00:00 to Monday 00:00 in TZ_NAME. Each finished week is closed once into
game_matches; the history of tasks and points stays. Nothing here talks to Telegram: callers get texts.
"""
import datetime as dt
import html
import logging

import db
import jokes

log = logging.getLogger("game")

ROUT_MARGIN = 5      # «Разгром»: won by 5 points or more (a usual week of a couple is a handful of tasks)
STREAK_WEEKS = 3     # «Победная серия»: three weekly wins in a row, calendar weeks back to back
LONG_HAUL_DAYS = 14  # «Победитель долгостроя»: a one-off task done 14+ days after it was written down

# code: (icon, name, what it is given for)
ACHIEVEMENTS = {
    "first_done": ("🌱", "Первое дело", "Семья впервые закрыла задачу в приложении."),
    "clean_sheet": ("🧼", "Чистый лист", "Целая календарная неделя без единой просроченной задачи."),
    "comeback": ("🔄", "Возвращение недели", "Закрыли задачу, за которую бытовой хаос уже успел получить очко."),
    "rout": ("💥", "Разгром", f"Выиграли неделю с отрывом от {ROUT_MARGIN} очков."),
    "streak": ("🔥", "Победная серия", f"Выиграли {STREAK_WEEKS} недели подряд."),
    "long_haul": ("🏗", "Победитель долгостроя", f"Закрыли задачу, которая ждала {LONG_HAUL_DAYS} дней и дольше."),
}

RULES = [
    "Семья получает +1, когда дело действительно сделано. Просроченное тоже: вернуться в игру можно всегда.",
    "Бытовой хаос получает +1, если срок дела прошёл, а оно не сделано. Один раз за дело, без штрафа за каждый день.",
    "Дела без срока и покупки хаосу очков не дают. Перенос срока до дедлайна — честный ход, очко хаосу не уходит.",
    "Матч длится неделю, с понедельника по воскресенье. В понедельник утром — итог и новый счёт с нуля.",
    "Вы играете вместе: счёт общий, кто сколько сделал, приложение не считает.",
]


def week_label(monday: dt.date) -> str:
    return f"{monday:%d.%m}–{monday + dt.timedelta(days=6):%d.%m}"


def score_line(family: int, chaos: int) -> str:
    return f"Семья {family} : {chaos} Бытовой хаос"


def status(family: int, chaos: int, days_left: int) -> str:
    if family > chaos:
        return "На этой неделе вы впереди!"
    if family < chaos:
        return f"Хаос пока впереди, но до конца недели {jokes.plural_days(days_left)}. Время отыграться!"
    if family == 0:
        return "Новый матч только начался. Первое сделанное дело — первое очко."
    return "Пока ничья. Одно дело — и вы впереди!"


def achievement_text(code: str, detail: str) -> str:
    icon, title, about = ACHIEVEMENTS[code]
    return f"🏅 Достижение «{title}» {icon}\n{about}" + (f"\n{html.escape(detail, quote=False)}" if detail else "")


def _unlock(couple_id: int, code: str, detail: str) -> list[str]:
    if db.unlock(couple_id, code, detail):
        log.info("couple %s unlocked %s (%s)", couple_id, code, detail)
        return [achievement_text(code, detail)]
    return []


def on_done(couple_id: int, row, instance: str, today: dt.date) -> tuple[bool, list[str], dict]:
    """The family's point for a completion that really happened on the server.
    Returns (scored now, achievement texts for the couple, this week's score)."""
    week = db.week_of(today).isoformat()
    got = db.score_family(couple_id, row["id"], instance, row["text"], week)
    if not got:
        log.info("family point for item %s [%s] is already counted, not again", row["id"], instance)
        return False, [], db.game_score(couple_id, week)
    log.info("family +1: couple %s, item %s [%s], week %s (%s)", couple_id, row["id"], instance, week, got)
    what = f"За дело «{row['text']}»."
    unlocked = _unlock(couple_id, "first_done", what)
    if db.has_chaos(couple_id, row["id"], instance):
        unlocked += _unlock(couple_id, "comeback", what)
    if not instance and (today - dt.date.fromisoformat(row["created"][:10])).days >= LONG_HAUL_DAYS:
        days = (today - dt.date.fromisoformat(row["created"][:10])).days
        unlocked += _unlock(couple_id, "long_haul", f"«{row['text']}» ждало {jokes.plural_days(days)}.")
    return True, unlocked, db.game_score(couple_id, week)


def on_undo(couple_id: int, item_id: int, instance: str) -> None:
    """«Вернуть»: the family's point for that completion is taken back; a repeated «Сделано» brings the same one back."""
    if db.revoke_family(couple_id, item_id, instance):
        log.info("family point taken back: couple %s, item %s [%s]", couple_id, item_id, instance)


def record_chaos(today: dt.date) -> None:
    n = db.record_chaos(today)
    if n:
        log.info("chaos +%s for tasks overdue before %s", n, today)


def _result(family: int, chaos: int) -> str:
    return "family" if family > chaos else "chaos" if chaos > family else "draw"


def close_weeks(today: dt.date) -> list[tuple[int, str]]:
    """Close every finished week that had points: (couple_id, message for its members), once per couple and week.
    One couple's failure is logged and does not stop the others."""
    record_chaos(today)  # Sunday's overdue tasks land in Sunday's week before that week is closed
    out = []
    for r in db.weeks_to_close(db.week_of(today).isoformat()):
        try:
            text = _close(r["couple_id"], r["week"])
        except Exception:
            log.exception("closing week %s of couple %s failed", r["week"], r["couple_id"])
            continue
        if text:
            out.append((r["couple_id"], text))
    return out


def _close(couple_id: int, week: str) -> str:
    s = db.game_score(couple_id, week)
    f, c = s["family"], s["chaos"]
    result = _result(f, c)
    if not db.close_match(couple_id, week, f, c, result):
        log.info("week %s of couple %s is already closed", week, couple_id)
        return ""
    log.info("week %s of couple %s closed %s:%s, %s", week, couple_id, f, c, result)
    monday = dt.date.fromisoformat(week)
    lines = {"family": jokes.WEEK_FAMILY, "chaos": jokes.WEEK_CHAOS, "draw": jokes.WEEK_DRAW}[result]
    head = jokes.pick(lines, monday.toordinal() // 7 + couple_id).format(f=f, c=c)
    parts = [f"<b>{head}</b>", f"Неделя {week_label(monday)}: {score_line(f, c)}."]
    label = f"Неделя {week_label(monday)}, {f}:{c}."
    unlocked = []
    if c == 0 and f >= 1 and week >= db.game_start():  # a whole week watched by the game, none overdue
        unlocked += _unlock(couple_id, "clean_sheet", label)
    if result == "family" and f - c >= ROUT_MARGIN:
        unlocked += _unlock(couple_id, "rout", label)
    if result == "family" and _streak(couple_id) >= STREAK_WEEKS:
        unlocked += _unlock(couple_id, "streak", f"{STREAK_WEEKS} победы подряд, последняя — неделя {week_label(monday)}.")
    parts += unlocked
    parts.append("Новый матч уже идёт, счёт 0:0.")
    return "\n\n".join(parts)


def _streak(couple_id: int) -> int:
    """Weekly wins in a row up to the latest closed week; a week without a match breaks the series."""
    n, expect = 0, None
    for m in db.matches(couple_id, 52):
        monday = dt.date.fromisoformat(m["week"])
        if m["result"] != "family" or (expect and monday != expect):
            break
        n, expect = n + 1, monday - dt.timedelta(days=7)
    return n


def current(couple_id: int, today: dt.date) -> dict:
    monday = db.week_of(today)
    s = db.game_score(couple_id, monday.isoformat())
    days_left = 7 - today.weekday()
    return {"week": monday.isoformat(), "label": week_label(monday), "family": s["family"], "chaos": s["chaos"],
            "days_left": days_left, "status": status(s["family"], s["chaos"], days_left)}


def season(couple_id: int, today: dt.date) -> dict:
    got = db.achievements(couple_id)
    history = [{"week": m["week"], "label": week_label(dt.date.fromisoformat(m["week"])), "family": m["family"],
                "chaos": m["chaos"], "result": m["result"]} for m in db.matches(couple_id)]
    return {
        "current": current(couple_id, today),
        "last": history[0] if history else None,
        "history": history,
        "streak": _streak(couple_id),
        "achievements": [{"code": code, "icon": icon, "title": title, "about": about,
                          "unlocked": got[code]["unlocked"] if code in got else None,
                          "detail": got[code]["detail"] if code in got else ""}
                         for code, (icon, title, about) in ACHIEVEMENTS.items()],
        "rules": RULES,
    }
