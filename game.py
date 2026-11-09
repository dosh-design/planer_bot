"""«Мы против забытых дел»: the couple's shared weekly score. Partners are one team, never rivals:
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
LONG_HAUL_DAYS = 14  # «Победитель долгостроя»: a one-off task done 14+ days after it was written down
BUSY_DAY = 5         # «Продуктивный день»: 5 tasks done in one day
WEEKEND = 4          # «Выходные с пользой»: 4 tasks done over one Saturday and Sunday
BIG_WEEK = 15        # «Рекордная неделя»: the family scored 15 points in a week
EARLY_HOUR, LATE_HOUR = 8, 23  # «Ранние пташки» before 8:00, «Ночная смена» from 23:00

TIERS = ("bronze", "silver", "gold")
TIER_NAMES = {"bronze": ("🥉", "Бронза"), "silver": ("🥈", "Серебро"), "gold": ("🥇", "Золото")}
TIER_TO = {"bronze": "до бронзы", "silver": "до серебра", "gold": "до золота"}

DEEDS = ("дело", "дела", "дел")
WEEKS = ("неделя", "недели", "недель")
# code: (icon, name, what counts, (bronze, silver, gold): how many times, word forms for the count).
# Every achievement is the couple's together: none of them looks at who of the two did what.
ACHIEVEMENTS = {
    "done_total": ("🤝", "Сделано вместе", "Дела, которые семья довела до конца.", (1, 50, 200), DEEDS),
    "cozy_home": ("🏡", "Уютный дом", "Сделанные обязанности по дому: мусор, полы, цветы по расписанию.", (5, 25, 100), DEEDS),
    "bills": ("💳", "Без пени", "Ежемесячные платежи, отмеченные в свой день или раньше.", (1, 6, 12),
              ("платёж", "платежа", "платежей")),
    "busy_day": ("⚡", "Продуктивный день", f"Дни, когда семья сделала {BUSY_DAY} дел и больше.", (1, 5, 15),
                 ("день", "дня", "дней")),
    "weekend": ("🛋", "Выходные с пользой", f"Выходные, за которые сделано {WEEKEND} дела и больше.", (1, 4, 12),
                ("выходные", "выходных", "выходных")),
    "every_day": ("📅", "Ни дня без дела", "Недели, где хоть одно дело сделано в каждый из семи дней.", (1, 4, 12), WEEKS),
    "clean_sheet": ("🧼", "Чистый лист", "Недели без единой просроченной задачи.", (1, 4, 12), WEEKS),
    "comeback": ("🔄", "Возвращение недели", "Сделанные дела, которые уже успели принести очко забытым делам.", (1, 5, 20), DEEDS),
    "long_haul": ("🏗", "Победитель долгостроя", f"Дела, которые ждали {LONG_HAUL_DAYS} дней и дольше, — и дождались.",
                  (1, 3, 10), DEEDS),
    "project_done": ("🏁", "Большое дело", "Проекты, где закрыт последний шаг.", (1, 3, 10),
                     ("проект", "проекта", "проектов")),
    "early_birds": ("🌅", "Ранние пташки", f"Дела, сделанные до {EARLY_HOUR}:00 утра.", (1, 10, 30), DEEDS),
    "night_owls": ("🦉", "Ночная смена", f"Дела, сделанные после {LATE_HOUR}:00.", (1, 10, 30), DEEDS),
    "rout": ("💥", "Разгром", f"Недели, выигранные с отрывом от {ROUT_MARGIN} очков.", (1, 3, 10), WEEKS),
    "big_week": ("🚀", "Рекордная неделя", f"Недели, где семья набрала {BIG_WEEK} очков и больше.", (1, 4, 12), WEEKS),
    "streak": ("🔥", "Победная серия", "Самая длинная серия побед неделя за неделей.", (3, 5, 10), WEEKS),
}

RULES = [
    "Семья получает +1, когда дело действительно сделано. Просроченное тоже: вернуться в игру можно всегда.",
    "Забытые дела получают +1, если срок дела прошёл, а оно не сделано. Один раз за дело, без штрафа за каждый день.",
    "Дела без срока и покупки очков не дают. Перенос срока до дедлайна — честный ход, очко не уходит.",
    "Матч длится неделю, с понедельника по воскресенье. В понедельник утром — итог и новый счёт с нуля.",
    "Вы играете вместе: счёт общий, кто сколько сделал, приложение не считает.",
]


def week_label(monday: dt.date) -> str:
    return f"{monday:%d.%m}–{monday + dt.timedelta(days=6):%d.%m}"


def score_line(family: int, chaos: int) -> str:
    return f"Мы {family} : {chaos} Забытые дела"


def status(family: int, chaos: int, days_left: int) -> str:
    if family > chaos:
        return "На этой неделе вы впереди!"
    if family < chaos:
        return f"Забытые дела пока впереди, но до конца недели {jokes.plural_days(days_left)}. Время отыграться!"
    if family == 0:
        return "Новый матч только начался. Первое сделанное дело — первое очко."
    return "Пока ничья. Одно дело — и вы впереди!"


def plural(n: int, forms: tuple[str, str, str]) -> str:
    one, few, many = forms
    k = n % 100
    word = many if 11 <= k <= 14 else one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many
    return f"{n} {word}"


def need_text(code: str, tier: str) -> str:
    _, _, _, needs, forms = ACHIEVEMENTS[code]
    return plural(needs[TIERS.index(tier)], forms) + (" подряд" if code == "streak" else "")


def achievement_text(code: str, tier: str, detail: str) -> str:
    icon, title, about, _, _ = ACHIEVEMENTS[code]
    medal, tier_name = TIER_NAMES[tier]
    text = f"{medal} {tier_name}: «{title}» {icon}\n{about} Уже {need_text(code, tier)}."
    nxt = TIERS.index(tier) + 1
    if nxt < len(TIERS):
        text += f" Дальше — {TIER_NAMES[TIERS[nxt]][1].lower()} за {need_text(code, TIERS[nxt])}."
    else:
        text += " Это высшая ступень!"
    return text + (f"\n{html.escape(detail, quote=False)}" if detail else "")


def _progress(couple_id: int, code: str, count: int, detail: str) -> list[str]:
    """Unlock every tier the count has reached; tell only about the highest one that is new now."""
    new = [t for t, need in zip(TIERS, ACHIEVEMENTS[code][3]) if count >= need and db.unlock(couple_id, f"{code}:{t}", detail)]
    if new:
        log.info("couple %s: %s reached %s (%s)", couple_id, code, new[-1], count)
        return [achievement_text(code, new[-1], detail)]
    return []


def _feat(couple_id: int, code: str, key: str, detail: str) -> list[str]:
    """One more time this achievement is earned (once per occasion), and the tiers it brings."""
    if not db.add_feat(couple_id, code, key):
        return []
    return _progress(couple_id, code, db.feat_counts(couple_id).get(code, 0), detail)


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
    now = db.local_now()
    key = f"{row['id']}:{instance}"
    unlocked = _progress(couple_id, "done_total", db.family_points(couple_id), what)
    if row["weekday"] is not None:
        unlocked += _progress(couple_id, "cozy_home", db.family_chores(couple_id), what)
    if (row["repeat"] or "").startswith("month:") and row["day"] and today <= dt.date.fromisoformat(row["day"]):
        unlocked += _feat(couple_id, "bills", key, f"«{row['text']}» — вовремя.")
    if db.has_chaos(couple_id, row["id"], instance):
        unlocked += _feat(couple_id, "comeback", key, what)
    if not instance and (today - dt.date.fromisoformat(row["created"][:10])).days >= LONG_HAUL_DAYS:
        days = (today - dt.date.fromisoformat(row["created"][:10])).days
        unlocked += _feat(couple_id, "long_haul", key, f"«{row['text']}» ждало {jokes.plural_days(days)}.")
    if row["project_id"]:
        left, steps = db.project_open_steps(row["project_id"], couple_id)
        project = db.project(row["project_id"], couple_id)
        if left == 0 and steps >= 2 and project:
            unlocked += _feat(couple_id, "project_done", str(row["project_id"]),
                              f"Проект «{project['name']}»: все {plural(steps, ('шаг', 'шага', 'шагов'))} позади.")
    if now.hour < EARLY_HOUR:
        unlocked += _feat(couple_id, "early_birds", key, f"«{row['text']}» в {now:%H:%M}.")
    elif now.hour >= LATE_HOUR:
        unlocked += _feat(couple_id, "night_owls", key, f"«{row['text']}» в {now:%H:%M}.")
    tomorrow = (today + dt.timedelta(days=1)).isoformat()
    if db.family_between(couple_id, today.isoformat(), tomorrow) >= BUSY_DAY:
        unlocked += _feat(couple_id, "busy_day", today.isoformat(), f"{today:%d.%m}: {BUSY_DAY} дел за день.")
    if today.weekday() >= 5:
        monday = db.week_of(today)
        if db.family_between(couple_id, (monday + dt.timedelta(days=5)).isoformat(), tomorrow if today.weekday() == 6
                             else (monday + dt.timedelta(days=7)).isoformat()) >= WEEKEND:
            unlocked += _feat(couple_id, "weekend", week, f"Выходные {monday + dt.timedelta(days=5):%d.%m}–{monday + dt.timedelta(days=6):%d.%m}.")
    if db.family_days(couple_id, week) == 7:
        unlocked += _feat(couple_id, "every_day", week, f"Неделя {week_label(db.week_of(today))}: каждый день с делом.")
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
        unlocked += _feat(couple_id, "clean_sheet", week, label)
    if result == "family" and f - c >= ROUT_MARGIN:
        unlocked += _feat(couple_id, "rout", week, label)
    if f >= BIG_WEEK:
        unlocked += _feat(couple_id, "big_week", week, label)
    if result == "family":
        n = _streak(couple_id)
        for k in range(1, n + 1):  # the count of this achievement is the longest series ever: one key per length
            db.add_feat(couple_id, "streak", str(k))
        unlocked += _progress(couple_id, "streak", db.feat_counts(couple_id).get("streak", 0),
                              f"{plural(n, WEEKS)} подряд, последняя — {week_label(monday)}.")
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
        "achievements": achievement_list(couple_id),
        "rules": RULES,
    }


def counts(couple_id: int) -> dict:
    n = db.feat_counts(couple_id)
    n["done_total"] = db.family_points(couple_id)
    n["cozy_home"] = db.family_chores(couple_id)
    return n


def achievement_list(couple_id: int) -> list[dict]:
    """Every achievement with its count, the tier reached (None, bronze, silver, gold) and what the next one needs."""
    got, n = db.achievements(couple_id), counts(couple_id)
    out = []
    for code, (icon, title, about, needs, forms) in ACHIEVEMENTS.items():
        tiers = [{"tier": t, "name": TIER_NAMES[t][1], "medal": TIER_NAMES[t][0], "need": need,
                  "need_text": need_text(code, t),
                  "unlocked": got[f"{code}:{t}"]["unlocked"] if f"{code}:{t}" in got else None,
                  "detail": got[f"{code}:{t}"]["detail"] if f"{code}:{t}" in got else ""}
                 for t, need in zip(TIERS, needs)]
        reached = [x for x in tiers if x["unlocked"]]
        nxt = next((x for x in tiers if not x["unlocked"]), None)
        top = reached[-1] if reached else None
        out.append({"code": code, "icon": icon, "title": title, "about": about,
                    "count": n.get(code, 0), "tier": top["tier"] if top else None, "tiers": tiers,
                    "next": {"tier": nxt["tier"], "name": nxt["name"], "need": nxt["need"], "need_text": nxt["need_text"]}
                    if nxt else None,
                    "unlocked": top["unlocked"] if top else None, "detail": top["detail"] if top else ""})
    return out
