"""The planner's logic, shared by the chat bot and the mini-app: adding items, warnings, reminders.

Nothing here talks to Telegram: functions return Out messages (chat id, text, buttons), and the bot sends them.
"""
import asyncio
import datetime as dt
import html
import logging
from dataclasses import dataclass, field

import db
import game
import jokes
import parser
from config import config

WEEKLY = ["каждый понедельник", "каждый вторник", "каждую среду", "каждый четверг", "каждую пятницу",
          "каждую субботу", "каждое воскресенье"]
HOT_AFTER_DAYS = 7
ICONS = {"task": "🔧", "buy": "🛒", "busy": "🚫", "event": "💞"}
log = logging.getLogger("planner")


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
    if row["project_id"] and not row["day"]:
        return False  # an undated step of a project waits for the project, it isn't a broken promise
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
        return _with_repeat(row, "просрочено со вчера" if late == 1 else f"просрочено на {jokes.plural_days(late)}")
    if row["kind"] == "task" and not row["day"]:
        hangs = hangs_days(row, today)
        if hangs >= 2:  # the quiet sarcasm of the list itself
            return f"висит {jokes.plural_days(hangs)}"
    return _with_repeat(row, parser.human_date(row["day"], row["time"], today, row["end_time"]))


def _with_repeat(row, text: str) -> str:
    if not row["repeat"]:
        return text
    return f"{text} · {REPEAT_NAMES.get(row['repeat'], 'каждый месяц')}"


REPEAT_NAMES = {"day": "каждый день", "week": "каждую неделю", "year": "каждый год"}


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


def bad_when_note(text: str, rows) -> str:
    """«31.02» or «в 25:00» in the phrase but nothing dated was saved: say so instead of a silent «записал»."""
    bad = parser.bad_when(text)
    if not bad or any(r["day"] or r["time"] for r in rows if r["kind"] != "buy"):
        return ""
    return f"⚠️ «{esc(bad)}» — такой даты или времени нет, поэтому записал без срока. Поправьте срок в карточке."


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


def add_parsed(uid: int, parsed: list, project_id: int | None = None) -> tuple[list, list[Out]]:
    """Save parsed items. project_id puts every task into that project (the mini-app's «add a step»)."""
    u = db.user(uid)
    p = db.partner(uid)
    rows, notes, fresh = [], [], []
    for it in parsed:
        if it.kind == "buy" and (same := db.open_buy(u["couple_id"], it.text)):
            rows.append(same)  # already on the list: a second «молоко» from either partner is the same milk
            continue
        assignee = assignee_for(uid, it.who) if it.kind != "buy" else None
        pid = project_id if it.kind == "task" and project_id else (
            db.project_by_name(u["couple_id"], uid, it.project) if it.kind == "task" and it.project else None)
        iid = db.add_item(u["couple_id"], uid, it.kind, it.text, assignee, it.date, it.time, it.weekly, it.end,
                          it.repeat, pid)
        rows.append(db.item(iid, u["couple_id"]))
        fresh.append(rows[-1])
    if fresh:
        db.touch(uid)
    if p:  # the partner hears only about what was really created now, about every new record, at once
        own = []
        for r in fresh:
            got = assigned_notes(uid, r)
            notes += got
            if not got and r["kind"] != "buy":
                own.append(r)
        if own:  # her own tasks and busy time: one short line, so the partner sees the whole picture without a ping per step
            what = "занятость" if all(r["kind"] == "busy" for r in own) else "себе"
            notes.append(Out(p["id"], f"📝 {esc(u['name'])} записал(а) {what}:\n" + "\n".join(line(r) for r in own)))
        buys = [r for r in fresh if r["kind"] == "buy"]
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


@dataclass
class Done:
    ok: bool
    notes: list[Out]
    scored: bool = False   # the family's point was counted by this very call
    toast: str = ""        # the short «Семья 1:0 — Бытовой хаос!» for the one who ticked it
    score: dict | None = None


def _instance(row, today: dt.date) -> str:
    """Which completion of the item a point is for: a chore's turn, a payment's month, or '' for a one-off task."""
    if row["weekday"] is not None:
        return turn_day(row["weekday"], today).isoformat()
    if row["repeat"] and row["day"]:
        return row["day"][:7] if row["repeat"].startswith("month:") else row["day"]
    return ""


def done(uid: int, item_id: int) -> tuple[bool, list[Out]]:
    d = complete(uid, item_id)
    return d.ok, d.notes


def complete(uid: int, item_id: int) -> Done:
    """Tick an item off; the partner hears about finished chores, and a task brings the family its point.
    The point and the messages come only from a change that really happened here (not from a second tap)."""
    u = db.user(uid)
    row = db.item(item_id, u["couple_id"]) if u and u["couple_id"] else None
    if not row:
        return Done(False, [])
    if row["status"] != "open":
        return Done(True, [])  # ticked twice: still done, the partner isn't told again
    chore = row["kind"] == "task" and row["weekday"] is not None
    if chore and chore_done(row):
        return Done(True, [])
    today = db.local_now().date()
    instance = _instance(row, today)
    if row["repeat"] and row["day"] and row["kind"] != "task":  # a birthday: on to its next date
        ok = db.move_repeat(item_id, u["couple_id"], parser.next_repeat(row["day"], row["repeat"]), done=True)
    elif row["repeat"] and row["day"]:  # a repeating task: this instance stays done in history, the next one appears
        ok = db.set_done(item_id, u["couple_id"])
        if ok:
            plan = row["plan_day"] or row["day"]
            nxt = parser.next_repeat(plan, row["repeat"])
            db.next_instance(row, nxt, nxt)
    else:
        ok = db.set_chore_done(item_id, u["couple_id"]) if chore else db.set_done(item_id, u["couple_id"])
    if not ok:
        return Done(True, [])  # the partner got there first: done, but not twice
    db.touch(uid)
    result = Done(True, [])
    unlocked = []
    if row["kind"] == "task":
        result.scored, unlocked, result.score = game.on_done(u["couple_id"], row, instance, today)
        if result.scored:
            result.toast = (f"{jokes.pick(jokes.WIN_DONE, item_id)} "
                            f"Неделя: {result.score['family']}:{result.score['chaos']}")
    p = db.partner(uid)
    if p and row["kind"] == "task":
        text = jokes.pick(jokes.DONE, item_id).format(who=esc(u["name"]), what=esc(row["text"]))
        if result.scored:
            text += f"\n{game.score_line(result.score['family'], result.score['chaos'])} на этой неделе."
        result.notes.append(Out(p["id"], text))
    for text in unlocked:
        result.notes += [Out(m["id"], text) for m in db.members(u["couple_id"])]
    return result


def undo_done(uid: int, item_id: int) -> bool:
    """Put an item ticked off by mistake back on the list (the partner is not told twice); its point goes back too."""
    u = db.user(uid)
    row = db.item(item_id, u["couple_id"]) if u and u["couple_id"] else None
    if not row:
        return False
    if row["repeat"] and row["day"] and row["kind"] == "task" and row["status"] == "done":
        instance = _instance(row, dt.date.fromisoformat(row["day"]))
        if not db.set_done(item_id, u["couple_id"], done=False):
            return False
        db.drop_next_instance(row, parser.next_repeat(row["plan_day"] or row["day"], row["repeat"]))
    elif row["repeat"] and row["day"]:  # back to the date it was ticked off from
        back = parser.prev_repeat(row["day"], row["repeat"])
        instance = back[:7] if row["repeat"].startswith("month:") else back
        if row["status"] != "open" or not row["done_at"] or not db.move_repeat(item_id, u["couple_id"], back, done=False):
            return False
    elif row["kind"] == "task" and row["weekday"] is not None:  # a weekly chore: only this week's tick goes
        instance = turn_day(row["weekday"], dt.date.fromisoformat(row["done_at"][:10])).isoformat() if row["done_at"] else ""
        if row["status"] != "open" or not row["done_at"] or not db.set_chore_done(item_id, u["couple_id"], done=False):
            return False
    else:
        instance = ""
        if row["status"] != "done" or not db.set_done(item_id, u["couple_id"], done=False):
            return False
    if row["kind"] == "task":
        game.on_undo(u["couple_id"], item_id, instance)
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
    for key in ("text", "note", "day", "time", "end", "repeat"):
        if key in changes and changes[key] is not None and not isinstance(changes[key], str):
            return False, "Неверный формат данных"
    if "text" in changes:
        text = " ".join(str(changes["text"] or "").split())
        if not text:
            return False, "Название не может быть пустым"
        fields["text"] = text[:300]
    if "note" in changes:
        fields["note"] = str(changes["note"] or "").strip()[:2000]
    if "remind" in changes:
        rb = changes["remind"]
        if rb is not None and (not isinstance(rb, int) or isinstance(rb, bool) or rb not in REMIND_CHOICES):
            return False, "Не понял, когда напомнить"
        fields["remind_before"] = rb
        if rb == db.HOURLY and not changes.get("day", row["day"]):
            return False, "Чтобы напоминать каждый час, укажите дату"
    if "repeat" in changes:
        rep = changes["repeat"] or None
        if rep is not None and rep not in ("day", "week", "month", "year"):
            return False, "Не понял, как повторять"
        if row["kind"] not in ("task", "event") or row["weekday"] is not None:
            return False, "Повтор здесь не настроить"
        fields["repeat"] = rep
    if "section" in changes:
        sid = changes["section"] or None
        if sid is not None and (not isinstance(sid, int) or isinstance(sid, bool) or not row["project_id"] or not (sec := db.section(sid, u["couple_id"]))
                                or sec["project_id"] != row["project_id"]):
            return False, "Этап не найден"
        fields["section_id"] = sid
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
    if "repeat" in fields:  # «каждый месяц» repeats on the day of the task's date
        day = fields.get("day", row["day"])
        if fields["repeat"] and not day:
            return False, "Для повтора нужна дата"
        if fields["repeat"] == "month":
            fields["repeat"] = f"month:{int(day[8:])}"
        if fields["repeat"] != row["repeat"]:
            fields["plan_day"] = day  # the series starts over from this instance
    if row["repeat"] == "year" and "day" in fields:  # a birthday moved is that date every year
        fields["repeat"] = "year" if fields["day"] else None
    if row["repeat"] and row["kind"] == "task" and "day" in fields and not fields["day"]:
        return False, "У повторяющегося дела должна быть дата"
    db.update_item(item_id, u["couple_id"], **fields)
    db.touch(uid)
    return True, ""


# What «Напомнить» offers: at the due time, N minutes before, or N days before (from the morning of the day).
REMIND_CHOICES = (-2, -1, 0, 15, 60, 180, 1440, 2880, 10080)


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


# ---------- projects ----------

PROJECT_IDEAS = ("Дом и ремонт", "Отпуск", "Платежи", "Подготовка к празднику")


def _couple(uid: int) -> int | None:
    u = db.user(uid)
    return u["couple_id"] if u else None


def create_project(uid: int, name: str, about: str = "") -> tuple[int | None, str]:
    """(project id, error). The name is the couple's own: two «Отпуск» would only confuse."""
    name, about = " ".join(str(name or "").split()), " ".join(str(about or "").split())
    if not name:
        return None, "Назовите проект"
    if len(name) > 60:
        return None, "Название длиннее 60 знаков"
    cid = _couple(uid)
    pid = db.create_project(cid, uid, name, about[:300]) if cid else None
    if not pid:
        return None, "Проект с таким названием уже есть"
    db.touch(uid)
    return pid, ""


def edit_project(uid: int, project_id: int, changes: dict) -> tuple[bool, str]:
    cid = _couple(uid)
    if not cid or not db.project(project_id, cid):
        return False, "Проект не найден"
    fields = {}
    if "name" in changes:
        fields["name"] = " ".join(str(changes["name"] or "").split())
        if not fields["name"]:
            return False, "Назовите проект"
        if len(fields["name"]) > 60:
            return False, "Название длиннее 60 знаков"
    if "about" in changes:
        fields["about"] = " ".join(str(changes["about"] or "").split())[:300]
    for key, field in (("start", "start_day"), ("end", "end_day")):
        if key in changes:
            value = changes[key] or None
            if value is not None and not _valid_day(value):
                return False, "Не понял дату"
            fields[field] = value
    p = db.project(project_id, cid)
    start, end = fields.get("start_day", p["start_day"]), fields.get("end_day", p["end_day"])
    if start and end and end < start:
        return False, "Конец проекта раньше начала"
    if not fields:
        return False, "Нечего менять"
    ok = db.update_project(project_id, cid, **fields)
    if ok is None:
        return False, "Проект с таким названием уже есть"
    db.touch(uid)
    return True, ""


def delete_project(uid: int, project_id: int) -> bool:
    """The steps stay in «Дела» as ordinary items."""
    cid = _couple(uid)
    ok = bool(cid and db.delete_project(project_id, cid))
    if ok:
        db.touch(uid)
    return ok


def add_section(uid: int, project_id: int, name: str) -> tuple[int | None, str]:
    cid = _couple(uid)
    if not cid or not db.project(project_id, cid):
        return None, "Проект не найден"
    name = " ".join(str(name or "").split())
    if not name:
        return None, "Назовите этап"
    if any(r["name"].casefold() == name.casefold() for r in db.sections(project_id, cid)):
        return None, "Такой этап уже есть"
    db.touch(uid)
    return db.add_section(project_id, cid, name), ""


def rename_section(uid: int, section_id: int, name: str) -> tuple[bool, str]:
    cid = _couple(uid)
    sec = db.section(section_id, cid) if cid else None
    if not sec:
        return False, "Этап не найден"
    name = " ".join(str(name or "").split())
    if not name:
        return False, "Назовите этап"
    if any(r["name"].casefold() == name.casefold() and r["id"] != section_id for r in db.sections(sec["project_id"], cid)):
        return False, "Такой этап уже есть"
    db.rename_section(section_id, cid, name)
    db.touch(uid)
    return True, ""


def delete_section(uid: int, section_id: int) -> bool:
    """The stage goes, its tasks stay in the project."""
    cid = _couple(uid)
    ok = bool(cid and db.delete_section(section_id, cid))
    if ok:
        db.touch(uid)
    return ok


def move(uid: int, item_id: int, step: int) -> bool:
    cid = _couple(uid)
    ok = bool(cid and step in (-1, 1) and db.move_item(item_id, cid, step))
    if ok:
        db.touch(uid)
    return ok


def add_to_project(uid: int, project_id: int, text: str, who: str = "me",
                   section_id: int | None = None) -> tuple[list, list[Out], str]:
    """A step typed inside a project: always a task of that project («купить плитку» is a step here, not
    a shopping line). Dates and times are read by the same rules as in the chat; who comes from the form."""
    cid = _couple(uid)
    if not cid or not db.project(project_id, cid):
        return [], [], "Проект не найден"
    text = " ".join(str(text or "").split())[:300]
    today = db.local_now().date()
    if not text:
        return [], [], "Напишите шаг"
    if parser.intent(text, today) == "chat":
        return [], [], "Это не похоже на шаг проекта. Напишите, что сделать."
    parsed = [it for it in parser.rules(text, today, in_project=True) if it.kind == "task"] or \
        [parser.Parsed("task", text[:1].upper() + text[1:])]
    if section_id is not None and (not (sec := db.section(section_id, cid)) or sec["project_id"] != project_id):
        return [], [], "Этап не найден"
    for it in parsed:
        it.who = who if who in ("me", "partner", "both") else "me"
    rows, notes = add_parsed(uid, parsed, project_id)
    if section_id is not None:
        for r in rows:
            db.update_item(r["id"], cid, section_id=section_id)
        rows = [db.item(r["id"], cid) for r in rows]
    return rows, notes, ""


def move_to_project(uid: int, item_id: int, project_id: int | None) -> bool:
    """Put a task into a project, or take it out (project_id None). Only tasks belong to projects."""
    cid = _couple(uid)
    row = db.item(item_id, cid) if cid else None
    if not row or row["kind"] != "task" or (project_id is not None and not db.project(project_id, cid)):
        return False
    ok = db.set_item_project(item_id, cid, project_id)
    if ok:
        db.touch(uid)
    return ok


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


STALE_REMINDER = dt.timedelta(hours=6)


def due(now: dt.datetime) -> list[Out]:
    """Exact-time reminders that have come."""
    out = []
    for row in db.due_reminders(now):
        db.mark_reminded(row["id"])
        if now - db.fire_at(row, config.morning_hour) > STALE_REMINDER:
            # The bot was down or the date was moved into the past: a «пора» hours late only confuses.
            # An overdue task still gets the overdue nudges.
            log.info("reminder for item %s is %s late, not sent", row["id"], now - db.fire_at(row, config.morning_hour))
            continue
        for uid in _targets(row):
            if row["remind_before"] and row["remind_before"] > 0:  # asked for ahead of time: the line says when it is due
                text = f"🔔 {esc(name(uid))}, заранее: {esc(row['text'])} — {esc(when_text(row, now.date()))}."
            else:
                text = _fmt(jokes.REMIND, row, uid, now.date(), row["id"])
            out.append(Out(uid, text, item_buttons(row)[:1]))
    for row in db.hourly_due(now):
        if not db.mark_hourly(row["id"], now.hour):
            continue
        first = row["reminded"] == 0
        for uid in _targets(row):
            text = (f"⏰ {esc(name(uid))}, сегодня: {esc(row['text'])}. Буду напоминать каждый час, пока не отметишь «Сделано»."
                    if first else f"⏰ {esc(name(uid))}, ещё раз: {esc(row['text'])}. Отметь «Сделано» — и я замолчу.")
            out.append(Out(uid, text, item_buttons(row)[:1]))
    return out


def morning(today: dt.date) -> list[Out]:
    """Daily digest per person plus nudges about overdue and long-hanging promises."""
    out = []
    t = today.isoformat()
    week_ahead = (today + dt.timedelta(days=7)).isoformat()
    try:  # last week's result comes first; a failure here must not cost the digest and the reminders
        for cid, text in game.close_weeks(today):
            out += [Out(m["id"], text) for m in db.members(cid)]
    except Exception:
        log.exception("closing the weekly matches failed")
    db.roll_yearly(today)
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
                if r["repeat"] == "year" and r["day"] == week_ahead:
                    lines.append(f"🎂 Через неделю: {esc(r['text'])} ({parser.human_date(r['day'], today=today)}). "
                                 "Подарок сам себя не купит.")
            for b in db.busy_on(cid, t):
                if b["assignee_id"] != person["id"]:
                    lines.append(f"🚫 {esc(name(b['assignee_id']))} сегодня занят(а): {esc(b['text'])}. Совместное не планируй.")
            deadline = [r for r in open_items if r["kind"] == "task" and r["day"] == t and r["weekday"] is None
                        and r["assignee_id"] in (person["id"], None)]
            mode = nudge_mode(person["id"])
            if deadline and mode in ("humor", "soft"):  # day 0: one line for the deadline day, not a message per task
                lines.append(jokes.pick(jokes.DUE_TODAY if mode == "humor" else jokes.DUE_TODAY_SOFT, today.toordinal()))
            if lines:
                out.append(Out(person["id"], "☀️ <b>Сегодня</b>\n" + "\n".join(lines)))
    out += nudges(today)
    return out


def overdue_stage(days: int) -> int:
    """Which overdue nudge is due: 0 none yet, 1 on day 2, 2 on day 4, 3 on day 7, then 4, 5, 6… on days 14, 28, 56…"""
    if days < 2:
        return 0
    if days < 4:
        return 1
    if days < 7:
        return 2
    stage, mark = 3, 14
    while days >= mark:
        stage, mark = stage + 1, mark * 2
    return stage


def _nudges_due(today: dt.date) -> list[tuple]:
    """(row, stage) pairs, the longest-hanging first; stage 0 is the old nudge for an undated promise."""
    due = []
    for row in db.nudge_candidates(today):
        since = dt.date.fromisoformat(row["day"] or row["created"][:10])
        stage = overdue_stage((today - since).days) if row["day"] else 0
        if row["day"] and stage <= row["nudges"]:
            continue  # this stage is already sent; after a pause only the latest one goes, never a backlog
        due.append((since, row["id"], row, stage))
    return [(row, stage) for _, _, row, stage in sorted(due, key=lambda d: d[:2])]


def nudge_mode(uid: int) -> str:
    u = db.user(uid)
    return u["nudge_mode"] if u and u["nudge_mode"] in jokes.NUDGE_MODES else "humor"


def _nudge_lines(mode: str, stage: int) -> list[str] | None:
    """What this person gets for this stage; None when their reminder setting skips it."""
    if stage:
        return jokes.overdue(stage, mode)
    return {"humor": jokes.NUDGE, "soft": jokes.OVERDUE_LONG_SOFT[1:]}.get(mode)  # an undated promise hanging


def nudges(today: dt.date) -> list[Out]:
    """Overdue and long-hanging promises, at most config.nudge_limit per person; the rest wait for the next days.
    Each person's reminder setting (nudge_mode) decides whether and in what tone they hear about it."""
    out, left, waiting = [], {}, {}
    for row, stage in _nudges_due(today):
        targets = [(uid, lines) for uid in _targets(row) if (lines := _nudge_lines(nudge_mode(uid), stage))]
        if any(left.setdefault(uid, config.nudge_limit) <= 0 for uid, _ in targets):
            for uid, _ in targets:
                waiting[uid] = waiting.get(uid, 0) + 1
            continue  # not marked, so it comes on a quieter morning
        if not db.mark_nudged(row["id"], today, stage or None):
            continue  # marked before sending: a re-run or a failed send never repeats the same stage
        log.info("nudge item %s stage %s to %s", row["id"], stage, [uid for uid, _ in targets] or "nobody (setting)")
        for uid, lines in targets:
            left[uid] -= 1
            out.append(Out(uid, _fmt(lines, row, uid, today, row["id"] + (stage or row["nudges"])), item_buttons(row)[:1]))
    for uid, n in waiting.items():
        out.append(Out(uid, f"⌛ В очереди ещё {jokes.plural(n, 'просроченное дело', 'просроченных дела', 'просроченных дел')},"
                            " о них напомню в другие дни. Весь список: /list"))
    return out


def tick(now: dt.datetime) -> list[Out]:
    """Called every minute by the bot."""
    out = due(now)
    try:  # the live score: a task whose day has passed gives the chaos its point right away
        game.record_chaos(now.date())
    except Exception:
        log.exception("recording the chaos points failed")
    if now.hour >= config.morning_hour and db.kv_get("morning") != now.date().isoformat():
        db.kv_set("morning", now.date().isoformat())
        out += morning(now.date())
    return out
