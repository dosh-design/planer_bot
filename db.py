"""SQLite storage. Dates are local "YYYY-MM-DD" and times "HH:MM" strings, so they sort and compare as text.

A couple is two users joined by a one-time invite (48 hours, one use, the latest link only). Everything else belongs to the couple:
    task  — a chore or promise («вкрутить лампочку»), assigned to one partner or to both
    buy   — a shopping list line
    busy  — one partner's own plans (football, баня): a date, or a weekday when it repeats every week
    event — a joint plan («к маме в субботу»); it is checked against the busy days
"""
import datetime as dt
import secrets
import sqlite3 as _sqlite
import sqlite3
from contextlib import closing
from zoneinfo import ZoneInfo

from config import config

KINDS = ("task", "buy", "busy", "event")


def _connect() -> sqlite3.Connection:
    config.data_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.data_dir / "family.sqlite3", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def local_now() -> dt.datetime:
    """The couple's wall-clock time: the server itself runs in UTC."""
    return dt.datetime.now(ZoneInfo(config.timezone)).replace(tzinfo=None, second=0, microsecond=0)


def init() -> None:
    with closing(_connect()) as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS couples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                created TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                couple_id INTEGER REFERENCES couples(id),
                created TEXT NOT NULL,
                nudge_mode TEXT NOT NULL DEFAULT 'humor'  -- overdue reminders: humor, soft, min or off
            );
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                couple_id INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
                kind TEXT NOT NULL,
                text TEXT NOT NULL,
                author_id INTEGER NOT NULL,
                assignee_id INTEGER,            -- NULL = both partners
                day TEXT,
                time TEXT,
                end_time TEXT,                  -- HH:MM when the plan is a span: «с 8 до 10»
                repeat TEXT,                    -- "month:21" payments, "year" birthdays; day is the next date
                project_id INTEGER,             -- a step of a project (projects.id); NULL for a plain item
                weekday INTEGER,                -- 0 = Monday; busy days and house chores that repeat every week
                status TEXT NOT NULL DEFAULT 'open',
                created TEXT NOT NULL,
                done_at TEXT,
                reminded INTEGER NOT NULL DEFAULT 0,
                nudges INTEGER NOT NULL DEFAULT 0,
                last_nudge TEXT
            );
            CREATE INDEX IF NOT EXISTS items_couple ON items(couple_id, status);
            -- A couple's bigger thing: «Ремонт кухни», «Отпуск». Its steps are items with project_id.
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                couple_id INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                about TEXT NOT NULL DEFAULT '',
                author_id INTEGER NOT NULL,
                created TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS projects_name ON projects(couple_id, name COLLATE NOCASE);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            -- One-time invites. couples.code above stays for old rows but no longer lets anyone in.
            CREATE TABLE IF NOT EXISTS invites (
                code TEXT PRIMARY KEY,
                couple_id INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
                created TEXT NOT NULL,
                expires TEXT NOT NULL,
                used_by INTEGER,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            -- Who did something on which day: no texts, only for "couples still active on day 14".
            -- «Семья против бытового хаоса»: one row per point. The unique key is the guard against counting a point
            -- twice (a second tap, a retried request, a restart, both partners at once). Rows outlive their items.
            CREATE TABLE IF NOT EXISTS game_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                couple_id INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
                side TEXT NOT NULL CHECK (side IN ('family', 'chaos')),
                item_id INTEGER NOT NULL,
                instance TEXT NOT NULL DEFAULT '',  -- '' a one-off task; "2026-10" a monthly payment; a chore's turn date
                text TEXT NOT NULL,                 -- the task as it was, for the history
                week TEXT NOT NULL,                 -- the match: Monday of the week, YYYY-MM-DD
                at TEXT NOT NULL,
                revoked INTEGER NOT NULL DEFAULT 0, -- a family point taken back by «Вернуть»
                UNIQUE (couple_id, side, item_id, instance)
            );
            CREATE INDEX IF NOT EXISTS game_events_week ON game_events(couple_id, week);
            -- Finished weekly matches. The key makes a week close (and its message go out) only once.
            CREATE TABLE IF NOT EXISTS game_matches (
                couple_id INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
                week TEXT NOT NULL,
                family INTEGER NOT NULL,
                chaos INTEGER NOT NULL,
                result TEXT NOT NULL CHECK (result IN ('family', 'chaos', 'draw')),
                closed TEXT NOT NULL,
                PRIMARY KEY (couple_id, week)
            );
            CREATE TABLE IF NOT EXISTS game_achievements (
                couple_id INTEGER NOT NULL REFERENCES couples(id) ON DELETE CASCADE,
                code TEXT NOT NULL,
                unlocked TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (couple_id, code)
            );
            CREATE TABLE IF NOT EXISTS activity (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                PRIMARY KEY (user_id, day)
            );
            """
        )
        # Bases made before these columns existed: add them, keep every row as it is.
        have = {r[1] for r in c.execute("PRAGMA table_info(items)")}
        for col, kind in (("end_time", "TEXT"), ("repeat", "TEXT"), ("project_id", "INTEGER")):
            if col not in have:
                c.execute(f"ALTER TABLE items ADD COLUMN {col} {kind}")
        if "nudge_mode" not in {r[1] for r in c.execute("PRAGMA table_info(users)")}:
            c.execute("ALTER TABLE users ADD COLUMN nudge_mode TEXT NOT NULL DEFAULT 'humor'")
        # The game counts from the day it appeared: tasks overdue before that are not the chaos's points.
        c.execute("INSERT OR IGNORE INTO kv (key, value) VALUES ('game_start', ?)", (local_now().date().isoformat(),))


def _stamp() -> str:
    return local_now().strftime("%Y-%m-%d %H:%M")


# ---------- users and couples ----------

def user(uid: int) -> sqlite3.Row | None:
    with closing(_connect()) as c:
        return c.execute("SELECT * FROM users WHERE id = ?", (uid,)).fetchone()


def save_user(uid: int, name: str) -> None:
    with closing(_connect()) as c:
        c.execute(
            "INSERT INTO users (id, name, created) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET name = excluded.name",
            (uid, name[:40], _stamp()),
        )


def couple_code(uid: int) -> str:
    """The couple's internal code (no longer an invite: see invite_code); a couple of one is created on first call."""
    u = user(uid)
    with closing(_connect()) as c:
        if u and u["couple_id"]:
            return c.execute("SELECT code FROM couples WHERE id = ?", (u["couple_id"],)).fetchone()["code"]
        code = secrets.token_urlsafe(6).replace("-", "x").replace("_", "y")
        cid = c.execute("INSERT INTO couples (code, created) VALUES (?, ?)", (code, _stamp())).lastrowid
        c.execute("UPDATE users SET couple_id = ? WHERE id = ?", (cid, uid))
        return code


def members(couple_id: int) -> list[sqlite3.Row]:
    with closing(_connect()) as c:
        return c.execute("SELECT * FROM users WHERE couple_id = ? ORDER BY created, id", (couple_id,)).fetchall()


INVITE_HOURS = 48


def invite_code(uid: int, fresh: bool = False) -> str | None:
    """A one-time invite into the user's couple: the active one, or a new one (fresh=True revokes the old).
    None when the couple already has two people."""
    couple_code(uid)
    cid = user(uid)["couple_id"]
    now = _stamp()
    with closing(_connect()) as c:
        if c.execute("SELECT COUNT(*) FROM users WHERE couple_id = ?", (cid,)).fetchone()[0] >= 2:
            return None
        if fresh:
            c.execute("UPDATE invites SET revoked = 1 WHERE couple_id = ? AND used_by IS NULL", (cid,))
        else:
            row = c.execute("SELECT code FROM invites WHERE couple_id = ? AND used_by IS NULL AND revoked = 0 AND expires > ?"
                            " ORDER BY created DESC LIMIT 1", (cid, now)).fetchone()
            if row:
                return row["code"]
        code = secrets.token_urlsafe(12)
        expires = (local_now() + dt.timedelta(hours=INVITE_HOURS)).strftime("%Y-%m-%d %H:%M")
        c.execute("INSERT INTO invites (code, couple_id, created, expires) VALUES (?, ?, ?, ?)", (code, cid, now, expires))
        return code


def partner(uid: int) -> sqlite3.Row | None:
    u = user(uid)
    if not u or not u["couple_id"]:
        return None
    return next((m for m in members(u["couple_id"]) if m["id"] != uid), None)


def join(uid: int, code: str) -> str:
    """Join a couple by a one-time invite. Returns:
    "ok"; "self" — already in that couple; "full" — two people there already; "taken" — the user is in another
    couple with a partner (no silent switch); "used", "expired", "revoked" — the link no longer works; "unknown".
    Runs as one write transaction, so two people opening the same link at once can't both get in."""
    with closing(_connect()) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            result = _join(c, uid, code)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
        return result


def _join(c: _sqlite.Connection, uid: int, code: str) -> str:
    inv = c.execute("SELECT * FROM invites WHERE code = ?", (code,)).fetchone()
    if not inv:
        return "unknown"
    cid = inv["couple_id"]
    ids = [r["id"] for r in c.execute("SELECT id FROM users WHERE couple_id = ?", (cid,))]
    if uid in ids:
        return "self"
    if inv["used_by"] is not None:
        return "used"
    if inv["revoked"]:
        return "revoked"
    if inv["expires"] <= _stamp():
        return "expired"
    if len(ids) >= 2:
        return "full"
    old = c.execute("SELECT couple_id FROM users WHERE id = ?", (uid,)).fetchone()
    old_cid = old["couple_id"] if old else None
    if old_cid and c.execute("SELECT COUNT(*) FROM users WHERE couple_id = ? AND id != ?", (old_cid, uid)).fetchone()[0]:
        return "taken"
    c.execute("UPDATE users SET couple_id = ? WHERE id = ?", (cid, uid))
    c.execute("UPDATE invites SET used_by = ? WHERE code = ?", (uid, code))
    c.execute("UPDATE invites SET revoked = 1 WHERE couple_id = ? AND used_by IS NULL", (cid,))  # the couple is full now
    # A couple of one that the newcomer had started alone: its items move over, the empty couple goes.
    if old_cid and old_cid != cid:
        # Projects move too; one with a name the couple already has merges into it.
        for pr in c.execute("SELECT id, name FROM projects WHERE couple_id = ?", (old_cid,)).fetchall():
            same = _same_name(c, cid, pr["name"])
            if same:
                c.execute("UPDATE items SET project_id = ? WHERE project_id = ?", (same, pr["id"]))
                c.execute("DELETE FROM projects WHERE id = ?", (pr["id"],))
            else:
                c.execute("UPDATE projects SET couple_id = ? WHERE id = ?", (cid, pr["id"]))
        c.execute("UPDATE items SET couple_id = ? WHERE couple_id = ?", (cid, old_cid))
        # The score and achievements of the one who started alone join the couple's; the rest goes with the old couple.
        for table in ("game_events", "game_matches", "game_achievements"):
            c.execute(f"UPDATE OR IGNORE {table} SET couple_id = ? WHERE couple_id = ?", (cid, old_cid))
        c.execute("DELETE FROM couples WHERE id = ?", (old_cid,))
    return "ok"


def delete_user(uid: int) -> None:
    """/delete: the user leaves; when nobody is left in the couple, its items go too."""
    u = user(uid)
    with closing(_connect()) as c:
        c.execute("DELETE FROM users WHERE id = ?", (uid,))
        c.execute("DELETE FROM activity WHERE user_id = ?", (uid,))
        if u and u["couple_id"]:
            left = c.execute("SELECT COUNT(*) FROM users WHERE couple_id = ?", (u["couple_id"],)).fetchone()[0]
            if left:
                c.execute("UPDATE items SET assignee_id = NULL WHERE couple_id = ? AND assignee_id = ?", (u["couple_id"], uid))
            else:
                c.execute("DELETE FROM items WHERE couple_id = ?", (u["couple_id"],))
                c.execute("DELETE FROM projects WHERE couple_id = ?", (u["couple_id"],))
                c.execute("DELETE FROM couples WHERE id = ?", (u["couple_id"],))


# ---------- items ----------

def add_item(couple_id: int, author_id: int, kind: str, text: str, assignee_id: int | None = None,
             day: str | None = None, time: str | None = None, weekday: int | None = None,
             end_time: str | None = None, repeat: str | None = None, project_id: int | None = None) -> int:
    assert kind in KINDS
    with closing(_connect()) as c:
        return c.execute(
            "INSERT INTO items (couple_id, kind, text, author_id, assignee_id, day, time, weekday, created, end_time,"
            " repeat, project_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (couple_id, kind, text[:300], author_id, assignee_id, day, time, weekday, _stamp(),
             end_time if time else None, repeat if day else None, project_id),
        ).lastrowid


def item(item_id: int, couple_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as c:
        return c.execute("SELECT * FROM items WHERE id = ? AND couple_id = ?", (item_id, couple_id)).fetchone()


def items(couple_id: int, kind: str | None = None, status: str = "open") -> list[sqlite3.Row]:
    sql = "SELECT * FROM items WHERE couple_id = ? AND status = ?"
    args: list = [couple_id, status]
    if kind:
        sql += " AND kind = ?"
        args.append(kind)
    # Dated first (soonest on top), then undated in the order they were added.
    sql += " ORDER BY day IS NULL, day, time IS NULL, time, id"
    with closing(_connect()) as c:
        return c.execute(sql, args).fetchall()


def set_done(item_id: int, couple_id: int, done: bool = True) -> bool:
    """Only a real change counts: of two partners ticking the same item at once, one gets True."""
    with closing(_connect()) as c:
        cur = c.execute(
            "UPDATE items SET status = ?, done_at = ? WHERE id = ? AND couple_id = ? AND status = ?",
            ("done" if done else "open", _stamp() if done else None, item_id, couple_id, "open" if done else "done"),
        )
        return cur.rowcount == 1


def update_item(item_id: int, couple_id: int, **fields) -> bool:
    """Change text/day/time/end_time; a moved date or time re-arms the exact-time reminder and the overdue nudges."""
    allowed = {k: v for k, v in fields.items() if k in ("text", "day", "time", "end_time", "repeat")}
    if not allowed:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    args = list(allowed.values())
    if "day" in allowed or "time" in allowed:
        sets += ", reminded = 0"
    if "day" in allowed:  # a new deadline starts the overdue nudges over (the CASE sees the old day)
        sets += ", nudges = CASE WHEN day IS ? THEN nudges ELSE 0 END, last_nudge = CASE WHEN day IS ? THEN last_nudge END"
        args += [allowed["day"], allowed["day"]]
    with closing(_connect()) as c:
        return c.execute(f"UPDATE items SET {sets} WHERE id = ? AND couple_id = ?",
                         (*args, item_id, couple_id)).rowcount == 1


def move_repeat(item_id: int, couple_id: int, day: str, done: bool) -> bool:
    """A monthly payment ticked off moves to its next date (or back, on undo) and stays open."""
    with closing(_connect()) as c:
        return c.execute("UPDATE items SET day = ?, done_at = ?, reminded = 0, nudges = 0, last_nudge = NULL"
                         " WHERE id = ? AND couple_id = ? AND repeat IS NOT NULL",
                         (day, _stamp() if done else None, item_id, couple_id)).rowcount == 1


def roll_yearly(today: dt.date) -> None:
    """Birthdays and anniversaries whose day has passed move to next year's date."""
    with closing(_connect()) as c:
        rows = c.execute("SELECT id, day FROM items WHERE repeat = 'year' AND status = 'open' AND day < ?",
                         (today.isoformat(),)).fetchall()
        for r in rows:
            d = dt.date.fromisoformat(r["day"])
            while d < today:
                try:
                    d = d.replace(year=d.year + 1)
                except ValueError:
                    d = dt.date(d.year + 1, 2, 28)
            c.execute("UPDATE items SET day = ?, reminded = 0 WHERE id = ?", (d.isoformat(), r["id"]))


# ---------- projects ----------

def _key(name: str) -> str:
    return " ".join(name.casefold().replace("ё", "е").split())  # SQLite's NOCASE knows only Latin letters


def _same_name(c, couple_id: int, name: str, but: int | None = None) -> int | None:
    rows = c.execute("SELECT id, name FROM projects WHERE couple_id = ?", (couple_id,)).fetchall()
    return next((r["id"] for r in rows if _key(r["name"]) == _key(name) and r["id"] != but), None)


def project_by_name(couple_id: int, author_id: int, name: str, create: bool = True) -> int | None:
    """The couple's project with this name (any case), made on first mention when create=True."""
    name = " ".join(name.split())[:60]
    with closing(_connect()) as c:
        found = _same_name(c, couple_id, name)
        if found or not create:
            return found
        return c.execute("INSERT INTO projects (couple_id, name, author_id, created) VALUES (?, ?, ?, ?)",
                         (couple_id, name, author_id, _stamp())).lastrowid


def create_project(couple_id: int, author_id: int, name: str, about: str = "") -> int | None:
    """None when the couple already has a project with this name."""
    try:
        with closing(_connect()) as c:
            if _same_name(c, couple_id, name):
                return None
            return c.execute("INSERT INTO projects (couple_id, name, about, author_id, created) VALUES (?, ?, ?, ?, ?)",
                             (couple_id, name[:60], about[:300], author_id, _stamp())).lastrowid
    except sqlite3.IntegrityError:
        return None


def project(project_id: int, couple_id: int) -> sqlite3.Row | None:
    with closing(_connect()) as c:
        return c.execute("SELECT * FROM projects WHERE id = ? AND couple_id = ?", (project_id, couple_id)).fetchone()


def update_project(project_id: int, couple_id: int, **fields) -> bool | None:
    """Rename or re-describe. None when the new name is taken by another project of the couple."""
    allowed = {k: v for k, v in fields.items() if k in ("name", "about")}
    if not allowed:
        return False
    try:
        with closing(_connect()) as c:
            if "name" in allowed and _same_name(c, couple_id, allowed["name"], but=project_id):
                return None
            return c.execute(f"UPDATE projects SET {', '.join(f'{k} = ?' for k in allowed)} WHERE id = ? AND couple_id = ?",
                             (*allowed.values(), project_id, couple_id)).rowcount == 1
    except sqlite3.IntegrityError:
        return None


def delete_project(project_id: int, couple_id: int) -> bool:
    """The project goes; its steps stay as ordinary items, nothing the couple wrote is lost."""
    with closing(_connect()) as c:
        c.execute("UPDATE items SET project_id = NULL WHERE project_id = ? AND couple_id = ?", (project_id, couple_id))
        return c.execute("DELETE FROM projects WHERE id = ? AND couple_id = ?", (project_id, couple_id)).rowcount == 1


def projects(couple_id: int) -> list[dict]:
    """The couple's projects with progress, ones with open steps first, then by creation."""
    with closing(_connect()) as c:
        rows = c.execute(
            "SELECT p.id, p.name, p.about, p.created,"
            " COUNT(i.id) AS total, COALESCE(SUM(i.status = 'done'), 0) AS done, COALESCE(SUM(i.status = 'open'), 0) AS open"
            " FROM projects p LEFT JOIN items i ON i.project_id = p.id AND i.couple_id = p.couple_id"
            " WHERE p.couple_id = ? GROUP BY p.id ORDER BY open = 0 AND total > 0, p.id",
            (couple_id,)).fetchall()
    return [{"id": r["id"], "name": r["name"], "about": r["about"], "total": r["total"], "done": r["done"],
             "open": r["open"]} for r in rows]


def project_items(project_id: int, couple_id: int) -> list[sqlite3.Row]:
    """All steps of a project, open ones first (by date), then done ones, latest first."""
    with closing(_connect()) as c:
        return c.execute(
            "SELECT * FROM items WHERE project_id = ? AND couple_id = ?"
            " ORDER BY status = 'done', CASE WHEN status = 'done' THEN done_at END DESC, day IS NULL, day, time, id",
            (project_id, couple_id)).fetchall()


def set_item_project(item_id: int, couple_id: int, project_id: int | None) -> bool:
    with closing(_connect()) as c:
        return c.execute("UPDATE items SET project_id = ? WHERE id = ? AND couple_id = ?",
                         (project_id, item_id, couple_id)).rowcount == 1


def last_item_id() -> int:
    with closing(_connect()) as c:
        return c.execute("SELECT COALESCE(MAX(id), 0) FROM items").fetchone()[0]


def open_buy(couple_id: int, text: str) -> sqlite3.Row | None:
    """An open shopping line with this text (any case), so the same thing isn't listed twice."""
    key = " ".join(text.casefold().replace("ё", "е").split())  # SQLite's lower() knows only Latin letters
    with closing(_connect()) as c:
        rows = c.execute("SELECT * FROM items WHERE couple_id = ? AND kind = 'buy' AND status = 'open'",
                         (couple_id,)).fetchall()
    return next((r for r in rows if " ".join(r["text"].casefold().replace("ё", "е").split()) == key), None)


def set_chore_done(item_id: int, couple_id: int, done: bool = True) -> bool:
    """A weekly chore stays on the list: «done» only marks this week's turn (done_at), «undo» clears it."""
    with closing(_connect()) as c:
        return c.execute("UPDATE items SET done_at = ? WHERE id = ? AND couple_id = ? AND weekday IS NOT NULL",
                         (_stamp() if done else None, item_id, couple_id)).rowcount == 1


def set_assignee(item_id: int, couple_id: int, assignee_id: int | None) -> bool:
    with closing(_connect()) as c:
        return c.execute("UPDATE items SET assignee_id = ? WHERE id = ? AND couple_id = ?",
                         (assignee_id, item_id, couple_id)).rowcount == 1


def delete_item(item_id: int, couple_id: int) -> bool:
    with closing(_connect()) as c:
        return c.execute("DELETE FROM items WHERE id = ? AND couple_id = ?", (item_id, couple_id)).rowcount == 1


def busy_on(couple_id: int, day: str) -> list[sqlite3.Row]:
    """Partners' own plans that fall on this date (one-off or weekly)."""
    wd = dt.date.fromisoformat(day).weekday()
    with closing(_connect()) as c:
        return c.execute(
            "SELECT * FROM items WHERE couple_id = ? AND kind = 'busy' AND status = 'open' AND (day = ? OR weekday = ?)",
            (couple_id, day, wd),
        ).fetchall()


def events_on(couple_id: int, day: str | None = None, weekday: int | None = None) -> list[sqlite3.Row]:
    """Joint plans on this date, or on any coming date with this weekday."""
    with closing(_connect()) as c:
        rows = c.execute(
            "SELECT * FROM items WHERE couple_id = ? AND kind = 'event' AND status = 'open' AND day IS NOT NULL",
            (couple_id,),
        ).fetchall()
    today = local_now().date().isoformat()
    return [r for r in rows if r["day"] >= today and (
        r["day"] == day or (weekday is not None and dt.date.fromisoformat(r["day"]).weekday() == weekday))]


def due_reminders(now: dt.datetime) -> list[sqlite3.Row]:
    """Tasks and joint plans with an exact time that has come and not been reminded yet."""
    stamp_day, stamp_time = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
    with closing(_connect()) as c:
        return c.execute(
            "SELECT * FROM items WHERE status = 'open' AND reminded = 0 AND kind IN ('task', 'event')"
            " AND day IS NOT NULL AND time IS NOT NULL AND (day < ? OR (day = ? AND time <= ?))",
            (stamp_day, stamp_day, stamp_time),
        ).fetchall()


def mark_reminded(item_id: int) -> None:
    with closing(_connect()) as c:
        c.execute("UPDATE items SET reminded = 1 WHERE id = ?", (item_id,))


def nudge_candidates(today: dt.date) -> list[sqlite3.Row]:
    """Open tasks at least two days past their date, or undated and hanging for a while and not nudged recently.

    For a dated task the planner picks the overdue stage; nudges holds the last stage sent.
    """
    late = (today - dt.timedelta(days=2)).isoformat()
    first = (today - dt.timedelta(days=config.nudge_after_days)).isoformat()
    again = (today - dt.timedelta(days=config.nudge_every_days)).isoformat()
    with closing(_connect()) as c:
        return c.execute(
            "SELECT * FROM items WHERE status = 'open' AND kind = 'task' AND weekday IS NULL"
            " AND NOT (day IS NULL AND project_id IS NOT NULL)"
            " AND ((day IS NOT NULL AND day <= ?)"
            "      OR (day IS NULL AND substr(created, 1, 10) <= ? AND (last_nudge IS NULL OR last_nudge <= ?)))",
            (late, first, again),
        ).fetchall()


def mark_nudged(item_id: int, today: dt.date, stage: int | None = None) -> bool:
    """Record a nudge before it is sent. With a stage, only a higher one counts, so a re-run sends nothing twice."""
    with closing(_connect()) as c:
        if stage is None:
            cur = c.execute("UPDATE items SET nudges = nudges + 1, last_nudge = ? WHERE id = ?",
                            (today.isoformat(), item_id))
        else:
            cur = c.execute("UPDATE items SET nudges = ?, last_nudge = ? WHERE id = ? AND nudges < ? AND status = 'open'",
                            (stage, today.isoformat(), item_id, stage))
        return cur.rowcount == 1


def kv_get(key: str) -> str | None:
    with closing(_connect()) as c:
        row = c.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None


def kv_set(key: str, value: str) -> None:
    with closing(_connect()) as c:
        c.execute("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (key, value))


def couples_with_members() -> list[int]:
    with closing(_connect()) as c:
        return [r[0] for r in c.execute("SELECT DISTINCT couple_id FROM users WHERE couple_id IS NOT NULL")]


def touch(uid: int) -> None:
    """Mark the user active today (added, ticked off, reassigned or deleted something)."""
    with closing(_connect()) as c:
        c.execute("INSERT OR IGNORE INTO activity (user_id, day) VALUES (?, ?)", (uid, local_now().date().isoformat()))


def retention(day_n: int = 14, window: int = 7) -> dict:
    """Couples of two that are at least day_n days old, and how many of them had both partners active
    in the last `window` days. The age is counted from the couple's creation."""
    today = local_now().date()
    oldest = (today - dt.timedelta(days=day_n)).isoformat()
    since = (today - dt.timedelta(days=window - 1)).isoformat()
    with closing(_connect()) as c:
        rows = c.execute(
            "SELECT co.id, (SELECT COUNT(DISTINCT a.user_id) FROM activity a JOIN users u ON u.id = a.user_id"
            "               WHERE u.couple_id = co.id AND a.day >= ?) AS active"
            " FROM couples co WHERE substr(co.created, 1, 10) <= ?"
            " AND (SELECT COUNT(*) FROM users WHERE couple_id = co.id) = 2",
            (since, oldest),
        ).fetchall()
    return {"couples": len(rows), "active": sum(1 for r in rows if r["active"] == 2)}


def stats() -> dict:
    with closing(_connect()) as c:
        one = lambda sql: c.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "users": one("SELECT COUNT(*) FROM users"),
            "couples": one("SELECT COUNT(*) FROM (SELECT couple_id FROM users WHERE couple_id IS NOT NULL"
                           " GROUP BY couple_id HAVING COUNT(*) = 2)"),
            "items": one("SELECT COUNT(*) FROM items"),
            "done": one("SELECT COUNT(*) FROM items WHERE status = 'done'"),
        }


# ---------- the game: family vs household chaos ----------

def week_of(day: dt.date) -> dt.date:
    """A match runs Monday 00:00 to the next Monday 00:00, local time (TZ_NAME)."""
    return day - dt.timedelta(days=day.weekday())


def game_start() -> str:
    return kv_get("game_start") or "9999-12-31"


def score_family(couple_id: int, item_id: int, instance: str, text: str, week: str) -> str:
    """A family point for this completion. Returns "new", "again" (a point taken back by «Вернуть» is back on)
    or "" (already counted: nothing changes)."""
    with closing(_connect()) as c:
        c.execute("BEGIN IMMEDIATE")
        try:
            got = c.execute("INSERT OR IGNORE INTO game_events (couple_id, side, item_id, instance, text, week, at)"
                            " VALUES (?, 'family', ?, ?, ?, ?, ?)",
                            (couple_id, item_id, instance, text, week, _stamp())).rowcount and "new"
            if not got:
                got = c.execute("UPDATE game_events SET revoked = 0, week = ?, at = ? WHERE couple_id = ? AND side = 'family'"
                                " AND item_id = ? AND instance = ? AND revoked = 1",
                                (week, _stamp(), couple_id, item_id, instance)).rowcount and "again"
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return got or ""


def revoke_family(couple_id: int, item_id: int, instance: str) -> bool:
    with closing(_connect()) as c:
        return c.execute("UPDATE game_events SET revoked = 1 WHERE couple_id = ? AND side = 'family' AND item_id = ?"
                         " AND instance = ? AND revoked = 0", (couple_id, item_id, instance)).rowcount == 1


def has_chaos(couple_id: int, item_id: int, instance: str) -> bool:
    with closing(_connect()) as c:
        return c.execute("SELECT 1 FROM game_events WHERE couple_id = ? AND side = 'chaos' AND item_id = ? AND instance = ?",
                         (couple_id, item_id, instance)).fetchone() is not None


def record_chaos(today: dt.date) -> int:
    """A chaos point for every open dated task whose day has passed, once per task (once per month for a payment).
    The match is the week of the deadline, so Sunday's task belongs to Sunday's week. Returns how many are new."""
    with closing(_connect()) as c:
        return c.execute(
            "INSERT OR IGNORE INTO game_events (couple_id, side, item_id, instance, text, week, at)"
            " SELECT couple_id, 'chaos', id, CASE WHEN repeat LIKE 'month:%' THEN substr(day, 1, 7) ELSE '' END, text,"
            "        date(day, '-6 days', 'weekday 1'), ?"
            " FROM items WHERE kind = 'task' AND status = 'open' AND weekday IS NULL AND day IS NOT NULL"
            " AND day < ? AND day >= ?",
            (_stamp(), today.isoformat(), game_start()),
        ).rowcount


def game_score(couple_id: int, week: str) -> dict:
    with closing(_connect()) as c:
        rows = c.execute("SELECT side, COUNT(*) AS n FROM game_events WHERE couple_id = ? AND week = ? AND revoked = 0"
                         " GROUP BY side", (couple_id, week)).fetchall()
    score = {"family": 0, "chaos": 0}
    score.update({r["side"]: r["n"] for r in rows})
    return score


def family_points(couple_id: int) -> int:
    with closing(_connect()) as c:
        return c.execute("SELECT COUNT(*) FROM game_events WHERE couple_id = ? AND side = 'family' AND revoked = 0",
                         (couple_id,)).fetchone()[0]


def weeks_to_close(before: str) -> list[sqlite3.Row]:
    """(couple_id, week) of past weeks that had points and have no result yet, oldest first."""
    with closing(_connect()) as c:
        return c.execute(
            "SELECT DISTINCT e.couple_id, e.week FROM game_events e"
            " WHERE e.week < ? AND NOT EXISTS (SELECT 1 FROM game_matches m WHERE m.couple_id = e.couple_id AND m.week = e.week)"
            " ORDER BY e.week, e.couple_id", (before,)).fetchall()


def close_match(couple_id: int, week: str, family: int, chaos: int, result: str) -> bool:
    """True only for the call that really closed the week."""
    with closing(_connect()) as c:
        return c.execute("INSERT OR IGNORE INTO game_matches (couple_id, week, family, chaos, result, closed)"
                         " VALUES (?, ?, ?, ?, ?, ?)", (couple_id, week, family, chaos, result, _stamp())).rowcount == 1


def matches(couple_id: int, limit: int = 12) -> list[sqlite3.Row]:
    with closing(_connect()) as c:
        return c.execute("SELECT * FROM game_matches WHERE couple_id = ? ORDER BY week DESC LIMIT ?",
                         (couple_id, limit)).fetchall()


def unlock(couple_id: int, code: str, detail: str) -> bool:
    """True only the first time: an achievement is given once."""
    with closing(_connect()) as c:
        return c.execute("INSERT OR IGNORE INTO game_achievements (couple_id, code, unlocked, detail) VALUES (?, ?, ?, ?)",
                         (couple_id, code, _stamp(), detail)).rowcount == 1


def achievements(couple_id: int) -> dict:
    with closing(_connect()) as c:
        return {r["code"]: r for r in c.execute("SELECT * FROM game_achievements WHERE couple_id = ?", (couple_id,))}


def set_nudge_mode(uid: int, mode: str) -> bool:
    with closing(_connect()) as c:
        return c.execute("UPDATE users SET nudge_mode = ? WHERE id = ?", (mode, uid)).rowcount == 1
