"""The planner's logic, shared by the chat bot and the mini-app: adding items, warnings, reminders.

Nothing here talks to Telegram: functions return Out messages (chat id, text, buttons), and the bot sends them.
"""
import datetime as dt
import html
from dataclasses import dataclass, field

import db
import jokes
import parser
from config import config

WEEKLY = ["каждый понедельник", "каждый вторник", "каждую среду", "каждый четверг", "каждую пятницу",
          "каждую субботу", "каждое воскресенье"]
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


def when_text(row, today: dt.date | None = None) -> str:
    today = today or db.local_now().date()
    if row["weekday"] is not None:
        return WEEKLY[row["weekday"]]
    if row["kind"] == "task" and not row["day"]:
        hangs = (today - dt.date.fromisoformat(row["created"][:10])).days
        if hangs >= 2:  # the quiet sarcasm of the list itself
            return f"висит {jokes.plural_days(hangs)}"
    return parser.human_date(row["day"], row["time"], today)


def line(row, today: dt.date | None = None) -> str:
    """«🔧 Вкрутить лампочку · Вася · сб 26.10»"""
    parts = [f"{ICONS[row['kind']]} {esc(row['text'])}"]
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
    out = []
    if row["kind"] == "event" and row["day"]:
        for b in db.busy_on(row["couple_id"], row["day"]):
            out.append(jokes.CONFLICT.format(day=parser.human_date(row["day"]), who=esc(name(b["assignee_id"])),
                                             what=esc(b["text"])))
    if row["kind"] == "busy" and (row["day"] or row["weekday"] is not None):
        for e in db.events_on(row["couple_id"], row["day"], row["weekday"]):
            out.append(jokes.BUSY_CLASH.format(day=parser.human_date(e["day"]), what=esc(e["text"])))
    return out


def add_text(uid: int, text: str, forwarded: str = "") -> tuple[list, list[Out]]:
    """Parse a phrase and save its items. Returns (saved rows, messages for the partner)."""
    u = db.user(uid)
    if not u:
        return [], []
    if not u["couple_id"]:
        db.couple_code(uid)
        u = db.user(uid)
    p = db.partner(uid)
    today = db.local_now().date()
    parsed = parser.parse(text, today, author=u["name"], partner=p["name"] if p else "партнёр", forwarded=forwarded)
    return add_parsed(uid, parsed)


def add_parsed(uid: int, parsed: list) -> tuple[list, list[Out]]:
    u = db.user(uid)
    p = db.partner(uid)
    rows, notes = [], []
    for it in parsed:
        assignee = assignee_for(uid, it.who) if it.kind != "buy" else None
        iid = db.add_item(u["couple_id"], uid, it.kind, it.text, assignee, it.date, it.time, it.weekly)
        rows.append(db.item(iid, u["couple_id"]))
    if p:
        mine = [r for r in rows if r["kind"] != "buy" and r["assignee_id"] in (p["id"], None)]
        for r in mine:
            notes.append(Out(p["id"], f"📌 {esc(u['name'])} записал(а) на тебя: {line(r)}\n"
                                      "Отвертеться не выйдет, бот уже запомнил.", item_buttons(r)[:1]))
        buys = [r for r in rows if r["kind"] == "buy"]
        if buys:
            notes.append(Out(p["id"], f"🛒 {esc(u['name'])} добавил(а) в покупки: "
                                      + ", ".join(esc(r["text"]) for r in buys)))
    return rows, notes


def done(uid: int, item_id: int) -> tuple[bool, list[Out]]:
    """Tick an item off; the partner hears about finished chores (that's the fun part)."""
    u = db.user(uid)
    row = db.item(item_id, u["couple_id"]) if u and u["couple_id"] else None
    if not row or not db.set_done(item_id, u["couple_id"]):
        return False, []
    notes = []
    p = db.partner(uid)
    if p and row["kind"] == "task":
        text = jokes.pick(jokes.DONE, item_id).format(who=esc(u["name"]), what=esc(row["text"]))
        notes.append(Out(p["id"], text))
    return True, notes


def reassign(uid: int, item_id: int, who: str) -> bool:
    u = db.user(uid)
    return bool(u and u["couple_id"] and db.set_assignee(item_id, u["couple_id"], assignee_for(uid, who)))


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
