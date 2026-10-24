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
                end_time TEXT,                  -- HH:MM when the plan is a span: «с 8 до 10»
                weekday INTEGER,                -- 0 = Monday; busy days and house chores that repeat every week
                status TEXT NOT NULL DEFAULT 'open',
                created TEXT NOT NULL,
                done_at TEXT,
                reminded INTEGER NOT NULL DEFAULT 0,
                nudges INTEGER NOT NULL DEFAULT 0,
                last_nudge TEXT
            );
            CREATE INDEX IF NOT EXISTS items_couple ON items(couple_id, status);
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
            CREATE TABLE IF NOT EXISTS activity (
                user_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                PRIMARY KEY (user_id, day)
            );
            """
        )
        # Bases made before spans existed: add the column, keep every row as it is.
        if "end_time" not in {r[1] for r in c.execute("PRAGMA table_info(items)")}:
            c.execute("ALTER TABLE items ADD COLUMN end_time TEXT")


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
        c.execute("UPDATE items SET couple_id = ? WHERE couple_id = ?", (cid, old_cid))
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
                c.execute("DELETE FROM couples WHERE id = ?", (u["couple_id"],))


# ---------- items ----------

def add_item(couple_id: int, author_id: int, kind: str, text: str, assignee_id: int | None = None,
             day: str | None = None, time: str | None = None, weekday: int | None = None,
             end_time: str | None = None) -> int:
    assert kind in KINDS
    with closing(_connect()) as c:
        return c.execute(
            "INSERT INTO items (couple_id, kind, text, author_id, assignee_id, day, time, weekday, created, end_time)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (couple_id, kind, text[:300], author_id, assignee_id, day, time, weekday, _stamp(),
             end_time if time else None),
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


def update_item(item_id: int, couple_id: int, **fields) -> bool:
    """Change text/day/time/end_time; a moved date or time re-arms the exact-time reminder."""
    allowed = {k: v for k, v in fields.items() if k in ("text", "day", "time", "end_time")}
    if not allowed:
        return False
    sets = ", ".join(f"{k} = ?" for k in allowed)
    if "day" in allowed or "time" in allowed:
        sets += ", reminded = 0"
    with closing(_connect()) as c:
        return c.execute(f"UPDATE items SET {sets} WHERE id = ? AND couple_id = ?",
                         (*allowed.values(), item_id, couple_id)).rowcount == 1


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
    """Open tasks that are overdue, or undated and hanging for a while, and not nudged recently."""
    first = (today - dt.timedelta(days=config.nudge_after_days)).isoformat()
    again = (today - dt.timedelta(days=config.nudge_every_days)).isoformat()
    t = today.isoformat()
    with closing(_connect()) as c:
        return c.execute(
            "SELECT * FROM items WHERE status = 'open' AND kind = 'task' AND weekday IS NULL"
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
