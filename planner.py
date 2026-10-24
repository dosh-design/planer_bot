"""The planner's logic, shared by the chat bot and the mini-app: adding items, warnings, reminders.

Nothing here talks to Telegram: functions return Out messages (chat id, text, buttons), and the bot sends them.
"""
import asyncio
import datetime as dt
import html
from dataclasses import dataclass, field

import db
import jokes
import parser
from config import config

WEEKLY = ["каждый понедельник", "каждый вторник", "каждую среду", "каждый четверг", "каждую пятницу",
          "каждую субботу", "каждое воскресенье"]
HOT_AFTER_DAYS = 7
ICONS = {"task": "🔧", "buy": "🛒", "busy": "🚫", "event": "💞"}


@dataclass
class Out:
    chat_id: int
    text: str
    buttons: list[list[tuple[str, str]]] = field(default_factory=list)  # rows of (label, callback data)


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def name(uid: int | None) -> str:
    if uid is None:
        return "оба"
    u = db.user(uid)
    return u["name"] if u else "—"


def assignee_for(uid: int, who: str) -> int | None:
    if who == "both":
        return None
    if who == "partner":
        p = db.partner(uid)
        return p["id"] if p else uid
    return uid


def hangs_days(row, today: dt.date) -> int:
    return (today - dt.date.fromisoformat(row["created"][:10])).days


def is_hot(row, today: dt.date | None = None) -> bool:
    """Burning: a chore past its date, or a promise without a date hanging longer than HOT_AFTER_DAYS.
    A joint plan whose day has passed is history, not a fire: nobody can still go to yesterday's cinema."""
    today = today or db.local_now().date()
    if row["kind"] not in ("task", "event") or row["status"] != "open" or row["weekday"] is not None:
        return False  # a weekly chore never burns: it comes back every week anyway
    if row["day"]:
        return row["kind"] == "task" and row["day"] < today.isoformat()
    return row["kind"] == "task" and hangs_days(row, today) >= HOT_AFTER_DAYS


def turn_day(weekday: int, today: dt.date) -> dt.date:
    """The date of this week's turn of a weekly chore: today or the latest such weekday before it."""
    return today - dt.timedelta(days=(today.weekday() - weekday) % 7)


def chore_done(row, today: dt.date | None = None) -> bool:
    """A weekly chore counts as done once someone ticked it on or after this week's turn."""
    today = today or db.local_now().date()
    return bool(row["kind"] == "task" and row["weekday"] is not None and row["done_at"]
                and row["done_at"][:10] >= turn_day(row["weekday"], today).isoformat())


def when_text(row, today: dt.date | None = None) -> str:
    today = today or db.local_now().date()
    if row["weekday"] is not None:
        span = parser.human_time(row["time"], row["end_time"])
        label = WEEKLY[row["weekday"]]
        if row["kind"] == "task":
            if chore_done(row, today):
                nxt = turn_day(row["weekday"], today) + dt.timedelta(days=7)
                label = f"сделано, дальше {parser.WD_SHORT[nxt.weekday()]} {nxt:%d.%m}"
            elif row["weekday"] == today.weekday():
                label = f"сегодня, {label}"
        return f"{label}, {span}" if row["end_time"] else (f"{label} {span}" if span else label)
    if row["kind"] in ("task", "event") and row["day"] and row["day"] < today.isoformat():
        late = (today - dt.date.fromisoformat(row["day"])).days
        if row["kind"] == "event":
            return "было вчера" if late == 1 else f"было {jokes.plural_days(late)} назад"
        return "просрочено со вчера" if late == 1 else f"просрочено на {jokes.plural_days(late)}"
    if row["kind"] == "task" and not row["day"]:
        hangs = hangs_days(row, today)
        if hangs >= 2:  # the quiet sarcasm of the list itself
            return f"висит {jokes.plural_days(hangs)}"
    return parser.human_date(row["day"], row["time"], today, row["end_time"])


def line(row, today: dt.date | None = None) -> str:
    """«🔧 Вкрутить лампочку · Вася · сб 26.10»"""
    icon = "🔥" if is_hot(row, today) else ("🏠" if row["kind"] == "task" and row["weekday"] is not None
                                           else ICONS[row["kind"]])
    parts = [f"{icon} {esc(row['text'])}"]
    if row["kind"] != "buy":
        parts.append(esc(name(row["assignee_id"])))
    w = when_text(row, today)
    if w:
        parts.append(w)
    return " · ".join(parts)


def item_buttons(row) -> list[list[tuple[str, str]]]:
    rows = [[("✅ Сделано", f"done:{row['id']}"), ("🗑 Удалить", f"del:{row['id']}")]]
    if row["kind"] in ("task", "event"):
        rows.append([("👤 Мне", f"who:{row['id']}:me"), ("👫 Партнёру", f"who:{row['id']}:partner"),
                     ("👥 Обоим", f"who:{row['id']}:both")])
    return rows


def warnings(row) -> list[str]:
    """Joint plan on someone's busy day, or a busy day on top of a joint plan."""
    out, today = [], db.local_now().date()
    if row["kind"] == "event" and row["day"]:
        for b in db.busy_on(row["couple_id"], row["day"]):
            out.append(jokes.CONFLICT.format(day=parser.human_date(row["day"], today=today), who=esc(name(b["assignee_id"])),
                                             what=esc(b["text"])))
    if row["kind"] == "busy" and (row["day"] or row["weekday"] is not None):
        for e in db.events_on(row["couple_id"], row["day"], row["weekday"]):
            out.append(jokes.BUSY_CLASH.format(day=parser.human_date(e["day"], today=today), what=esc(e["text"])))
    return out


def _parse_args(uid: int, text: str, forwarded: str) -> dict | None:
    u = db.user(uid)
    if not u:
        return None
    if not u["couple_id"]:
        db.couple_code(uid)
    p = db.partner(uid)
    return dict(text=text, today=db.local_now().date(), author=u["name"], partner=p["name"] if p else "партнёр",
                forwarded=forwarded)


def add_text(uid: int, text: str, forwarded: str = "", allow_model: bool = True) -> tuple[list, list[Out]]:
    """Parse a phrase and save its items. Returns (saved rows, messages for the partner).
    Blocking when the text model is on: the bot and the mini-app use add_text_async.
    Nothing is saved and nobody is notified for small talk or unclear fragments (parser.intent)."""
    args = _parse_args(uid, text, forwarded)
    if not args:
        return [], []
    return add_parsed(uid, parser.parse(**args, allow_model=allow_model))


def model_budget(uid: int) -> bool:
    """Count one text-model call for today; False once the person is over AI_DAILY_LIMIT."""
    key = f"ai:{uid}:{db.local_now().date().isoformat()}"
    used = int(db.kv_get(key) or 0)
    if used >= config.ai_daily_limit:
        return False
    db.kv_set(key, str(used + 1))
    return True


async def add_text_async(uid: int, text: str, forwarded: str = "") -> tuple[list, list[Out]]:
    """add_text for the event loop: the model call runs in a worker thread, so a slow Yandex answer
    holds up only this phrase, not other people's messages, the mini-app or the reminders."""
    args = _parse_args(uid, text, forwarded)
    if not args:
        return [], []
    use_model = (config.ai_enabled and parser.intent(text, args["today"]) == "do" and model_budget(uid))
    parsed = await asyncio.to_thread(parser.parse, **args, allow_model=use_model)
    return add_parsed(uid, parsed)


def add_parsed(uid: int, parsed: list) -> tuple[list, list[Out]]:
    u = db.user(uid)
    p = db.partner(uid)
    rows, notes = [], []
    for it in parsed:
        assignee = assignee_for(uid, it.who) if it.kind != "buy" else None
        iid = db.add_item(u["couple_id"], uid, it.kind, it.text, assignee, it.date, it.time, it.weekly, it.end)
        rows.append(db.item(iid, u["couple_id"]))
    if rows:
        db.touch(uid)
    if p:
        for r in rows:
            notes += assigned_notes(uid, r)
        buys = [r for r in rows if r["kind"] == "buy"]
        if buys:
            notes.append(Out(p["id"], f"🛒 {esc(u['name'])} добавил(а) в покупки: "
                                      + ", ".join(esc(r["text"]) for r in buys)))
    return rows, notes


def assigned_notes(uid: int, row) -> list[Out]:
    """Tell the partner that something landed on them (or on both of them); same text from the chat and the mini-app."""
    u, p = db.user(uid), db.partner(uid)
    if not p or row["kind"] == "buy" or row["assignee_id"] not in (p["id"], None):
        return []
    whom = "на тебя" if row["assignee_id"] == p["id"] else "на вас обоих"
    return [Out(p["id"], f"📌 {esc(u['name'])} записал(а) {whom}: {line(row)}\n"
                         "Отвертеться не выйдет, бот уже запомнил.", item_buttons(row)[:1])]


def done(uid: int, item_id: int) -> tuple[bool, list[Out]]:
    """Tick an item off; the partner hears about finished chores (that's the fun part)."""
    u = db.user(uid)
    row = db.item(item_id, u["couple_id"]) if u and u["couple_id"] else None
    if not row:
        return False, []
    if row["status"] != "open":
        return True, []  # ticked twice: still done, the partner isn't told again
    chore = row["kind"] == "task" and row["weekday"] is not None
    if chore and chore_done(row):
        return True, []
    if not (db.set_chore_done(item_id, u["couple_id"]) if chore else db.set_done(item_id, u["couple_id"])):
        return False, []
    db.touch(uid)
    notes = []
    p = db.partner(uid)
    if p and row["kind"] == "task":
        text = jokes.pick(jokes.DONE, item_id).format(who=esc(u["name"]), what=esc(row["text"]))
        notes.append(Out(p["id"], text))
    return True, notes


def undo_done(uid: int, item_id: int) -> bool:
    """Put an item ticked off by mistake back on the list (the partner is not told twice)."""
    u = db.user(uid)
    row = db.item(item_id, u["couple_id"]) if u and u["couple_id"] else None
    if not row:
        return False
    if row["kind"] == "task" and row["weekday"] is not None:  # a weekly chore: only this week's tick goes
        if row["status"] != "open" or not row["done_at"] or not db.set_chore_done(item_id, u["couple_id"], done=False):
            return False
    elif row["status"] != "done" or not db.set_done(item_id, u["couple_id"], done=False):
        return False
    db.touch(uid)
    return True


def edit(uid: int, item_id: int, changes: dict) -> tuple[bool, str]:
    """Fix the text, the date or the time of an open item. Returns (ok, error text for the user).
    A new date or time arms the exact-time reminder again."""
    u = db.user(uid)
    row = db.item(item_id, u["couple_id"]) if u and u["couple_id"] else None
    if not row or row["status"] != "open":
        return False, "Запись не найдена"
    fields = {}
    if "text" in changes:
        text = " ".join(str(changes["text"] or "").split())
        if not text:
            return False, "Название не может быть пустым"
        fields["text"] = text[:300]
    for key, field, check in (("day", "day", _valid_day), ("time", "time", _valid_time), ("end", "end_time", _valid_time)):
        if key in changes:
            value = changes[key] or None
            if value is not None and not check(value):
                return False, "Не понял дату" if key == "day" else "Время нужно в виде 18:30"
            fields[field] = value
    start = fields.get("time", row["time"])
    if not start and "end_time" not in fields and row["end_time"]:
        fields["end_time"] = None  # the start is gone, so is the span
    end = fields.get("end_time", row["end_time"])
    if end and not start:
        return False, "Укажите, со скольки"
    if end and end <= start:
        return False, "Конец должен быть позже начала"
    if row["weekday"] is not None and ("day" in fields or "time" in fields or "end_time" in fields):
        return False, "Повторяющееся дело меняется сообщением боту"
    if "time" in fields and fields["time"] and not fields.get("day", row["day"]):
        return False, "Время без даты не сработает: укажите и дату"
    if not fields:
        return False, "Нечего менять"
    db.update_item(item_id, u["couple_id"], **fields)
    db.touch(uid)
    return True, ""


def _valid_day(value) -> bool:
    try:
        return bool(dt.date.fromisoformat(str(value))) and len(str(value)) == 10
    except ValueError:
        return False


def _valid_time(value) -> bool:
    try:
        return bool(dt.datetime.strptime(str(value), "%H:%M")) and len(str(value)) == 5
    except ValueError:
        return False


def reassign(uid: int, item_id: int, who: str) -> bool:
    u = db.user(uid)
    ok = bool(u and u["couple_id"] and db.set_assignee(item_id, u["couple_id"], assignee_for(uid, who)))
    if ok:
        db.touch(uid)
    return ok


def remove(uid: int, item_id: int) -> bool:
    u = db.user(uid)
    ok = bool(u and u["couple_id"] and db.delete_item(item_id, u["couple_id"]))
    if ok:
        db.touch(uid)
    return ok


# ---------- reminders ----------

def _targets(row) -> list[int]:
    if row["assignee_id"] is not None:
        return [row["assignee_id"]]
    return [m["id"] for m in db.members(row["couple_id"])]


def _fmt(template: str, row, who_id: int, today: dt.date, seed: int) -> str:
    author = row["author_id"]
    author_name = name(author) if author != who_id else "совесть"
    since = dt.date.fromisoformat(row["day"] or row["created"][:10])
    n = max((today - since).days, 1)
    return jokes.pick(template, seed).format(
        who=esc(name(who_id)), what=esc(row["text"]), author=esc(author_name),
        days=jokes.plural_days(n), days_n=n, when=parser.human_date(row["day"], today=today),
    )


def due(now: dt.datetime) -> list[Out]:
    """Exact-time reminders that have come."""
    out = []
    for row in db.due_reminders(now):
        db.mark_reminded(row["id"])
        for uid in _targets(row):
            out.append(Out(uid, _fmt(jokes.REMIND, row, uid, now.date(), row["id"]), item_buttons(row)[:1]))
    return out


def morning(today: dt.date) -> list[Out]:
    """Daily digest per person plus nudges about overdue and long-hanging promises."""
    out = []
    t = today.isoformat()
    for cid in db.couples_with_members():
        people = db.members(cid)
        open_items = db.items(cid)
        for person in people:
            lines = []
            for r in open_items:
                if r["kind"] in ("task", "event") and r["day"] == t and r["assignee_id"] in (person["id"], None):
                    lines.append(line(r, today))
                elif (r["kind"] == "task" and r["weekday"] == today.weekday() and not chore_done(r, today)
                      and r["assignee_id"] in (person["id"], None)):
                    lines.append(line(r, today))
            for b in db.busy_on(cid, t):
                if b["assignee_id"] != person["id"]:
                    lines.append(f"🚫 {esc(name(b['assignee_id']))} сегодня занят(а): {esc(b['text'])}. Совместное не планируй.")
            if lines:
                out.append(Out(person["id"], "☀️ <b>Сегодня</b>\n" + "\n".join(lines)))
    for row in db.nudge_candidates(today):
        db.mark_nudged(row["id"], today)
        template = jokes.OVERDUE if row["day"] else jokes.NUDGE
        for uid in _targets(row):
            out.append(Out(uid, _fmt(template, row, uid, today, row["id"] + row["nudges"]), item_buttons(row)[:1]))
    return out


def tick(now: dt.datetime) -> list[Out]:
    """Called every minute by the bot."""
    out = due(now)
    if now.hour >= config.morning_hour and db.kv_get("morning") != now.date().isoformat():
        db.kv_set("morning", now.date().isoformat())
        out += morning(now.date())
    return out
