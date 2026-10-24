"""SQLite storage. Dates are local "YYYY-MM-DD" and times "HH:MM" strings, so they sort and compare as text.

A couple is two users joined by an invite code. Everything else belongs to the couple:
    task  — a chore or promise («вкрутить лампочку»), assigned to one partner or to both
    buy   — a shopping list line
    busy  — one partner's own plans (football, баня): a date, or a weekday when it repeats every week
    event — a joint plan («к маме в субботу»); it is checked against the busy days
"""
import datetime as dt
import secrets
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
                created TEXT NOT NULL
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
                weekday INTEGER,                -- 0 = Monday; busy days that repeat every week
                status TEXT NOT NULL DEFAULT 'open',
                created TEXT NOT NULL,
                done_at TEXT,
                reminded INTEGER NOT NULL DEFAULT 0,
                nudges INTEGER NOT NULL DEFAULT 0,
                last_nudge TEXT
            );
            CREATE INDEX IF NOT EXISTS items_couple ON items(couple_id, status);
            CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )


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
    """The user's invite code; a couple of one is created on first call."""
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


def partner(uid: int) -> sqlite3.Row | None:
    u = user(uid)
    if not u or not u["couple_id"]:
        return None
    return next((m for m in members(u["couple_id"]) if m["id"] != uid), None)


def join(uid: int, code: str) -> str:
    """Join the couple with this invite code. Returns "ok", "self", "full" or "unknown"."""
    with closing(_connect()) as c:
        row = c.execute("SELECT id FROM couples WHERE code = ?", (code,)).fetchone()
        if not row:
            return "unknown"
        cid = row["id"]
        ids = [r["id"] for r in c.execute("SELECT id FROM users WHERE couple_id = ?", (cid,))]
        if uid in ids:
            return "self"
        if len(ids) >= 2:
            return "full"
        old = c.execute("SELECT couple_id FROM users WHERE id = ?", (uid,)).fetchone()
        c.execute("UPDATE users SET couple_id = ? WHERE id = ?", (cid, uid))
        # A couple of one that the newcomer had started alone: its items move over, the empty couple goes.
        if old and old["couple_id"] and old["couple_id"] != cid:
            left = c.execute("SELECT COUNT(*) FROM users WHERE couple_id = ?", (old["couple_id"],)).fetchone()[0]
            if not left:
                c.execute("UPDATE items SET couple_id = ? WHERE couple_id = ?", (cid, old["couple_id"]))
                c.execute("DELETE FROM couples WHERE id = ?", (old["couple_id"],))
        return "ok"


def delete_user(uid: int) -> None:
    """/delete: the user leaves; when nobody is left in the couple, its items go too."""
    u = user(uid)
    with closing(_connect()) as c:
        c.execute("DELETE FROM users WHERE id = ?", (uid,))
        if u and u["couple_id"]:
            left = c.execute("SELECT COUNT(*) FROM users WHERE couple_id = ?", (u["couple_id"],)).fetchone()[0]
            if left:
                c.execute("UPDATE items SET assignee_id = NULL WHERE couple_id = ? AND assignee_id = ?", (u["couple_id"], uid))
            else:
                c.execute("DELETE FROM items WHERE couple_id = ?", (u["couple_id"],))
                c.execute("DELETE FROM couples WHERE id = ?", (u["couple_id"],))


# ---------- items ----------

def add_item(couple_id: int, author_id: int, kind: str, text: str, assignee_id: int | None = None,
             day: str | None = None, time: str | None = None, weekday: int | None = None) -> int:
    assert kind in KINDS
    with closing(_connect()) as c:
        return c.execute(
            "INSERT INTO items (couple_id, kind, text, author_id, assignee_id, day, time, weekday, created)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (couple_id, kind, text[:300], author_id, assignee_id, day, time, weekday, _stamp()),
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
    with closing(_connect()) as c:
        cur = c.execute(
            "UPDATE items SET status = ?, done_at = ? WHERE id = ? AND couple_id = ?",
            ("done" if done else "open", _stamp() if done else None, item_id, couple_id),
        )
        return cur.rowcount == 1


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
    """Open tasks that are overdue, or undated and hanging for a while, and not nudged recently."""
    first = (today - dt.timedelta(days=config.nudge_after_days)).isoformat()
    again = (today - dt.timedelta(days=config.nudge_every_days)).isoformat()
    t = today.isoformat()
    with closing(_connect()) as c:
        return c.execute(
            "SELECT * FROM items WHERE status = 'open' AND kind = 'task'"
            " AND ((day IS NOT NULL AND day < ?) OR (day IS NULL AND substr(created, 1, 10) <= ?))"
            " AND (last_nudge IS NULL OR last_nudge <= ?)",
            (t, first, again),
        ).fetchall()


def mark_nudged(item_id: int, today: dt.date) -> None:
    with closing(_connect()) as c:
        c.execute("UPDATE items SET nudges = nudges + 1, last_nudge = ? WHERE id = ?", (today.isoformat(), item_id))


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
