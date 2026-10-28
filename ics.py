"""Calendar import: an .ics file or link (iPhone / iCloud, Google, any calendar app) becomes the couple's plans.

Only what the planner can show is taken: upcoming events (the past is skipped), birthdays and other yearly dates
as «каждый год», weekly ones as a weekday. Everything is read in the planner's time zone.
"""
import datetime as dt
import ipaddress
import re
import socket
from dataclasses import dataclass
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AHEAD_DAYS = 365     # one year forward is plenty for a family planner
MAX_EVENTS = 300     # one import never floods the lists
MAX_BYTES = 3 * 1024 * 1024


@dataclass
class Event:
    uid: str
    text: str
    day: str | None          # ISO date; None for a weekly busy slot
    time: str | None = None  # HH:MM
    end: str | None = None
    weekday: int | None = None
    repeat: str | None = None  # "year"
    all_day: bool = False


def _lines(text: str) -> list[str]:
    # RFC 5545: a line starting with a space or a tab continues the previous one
    out: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        elif raw:
            out.append(raw)
    return out


def _unescape(v: str) -> str:
    return re.sub(r"\\([\\;,nN])", lambda m: "\n" if m[1] in "nN" else m[1], v).strip()


def _when(value: str, params: dict, tz: ZoneInfo) -> tuple[dt.date, str | None, bool]:
    """(local date, local HH:MM or None, all-day?)"""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return dt.datetime.strptime(value[:8], "%Y%m%d").date(), None, True
    moment = dt.datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        moment = moment.replace(tzinfo=dt.timezone.utc)
    elif "TZID" in params:
        try:
            moment = moment.replace(tzinfo=ZoneInfo(params["TZID"].strip('"')))
        except (ZoneInfoNotFoundError, ValueError):
            moment = moment.replace(tzinfo=tz)  # a Windows zone name and the like: read as our own time
    else:
        moment = moment.replace(tzinfo=tz)  # «floating» time: the phone's own, which is ours
    local = moment.astimezone(tz)
    return local.date(), local.strftime("%H:%M"), False


def parse(text: str, today: dt.date, tz_name: str) -> list[Event]:
    tz = ZoneInfo(tz_name)
    events, cur, depth = [], None, 0
    for line in _lines(text):
        name, _, value = line.partition(":")
        key, *raw_params = name.split(";")
        key = key.upper()
        params = dict(p.split("=", 1) for p in raw_params if "=" in p)
        params = {k.upper(): v for k, v in params.items()}
        if key == "BEGIN" and value.upper() == "VEVENT":
            cur, depth = {}, 0
        elif key == "BEGIN" and cur is not None:
            depth += 1  # VALARM inside the event: its fields are not the event's
        elif key == "END" and cur is not None and depth:
            depth -= 1
        elif key == "END" and value.upper() == "VEVENT" and cur is not None:
            ev = _event(cur, today, tz)
            if ev:
                events.append(ev)
            cur = None
        elif cur is not None and not depth and key not in cur:
            cur[key] = (value, params)
    events.sort(key=lambda e: (e.day or "", e.time or ""))
    return events[:MAX_EVENTS]


def _event(f: dict, today: dt.date, tz: ZoneInfo) -> Event | None:
    if "DTSTART" not in f or f.get("STATUS", ("",))[0].upper() == "CANCELLED":
        return None
    title = _unescape(f.get("SUMMARY", ("",))[0])[:200] or "Событие из календаря"
    try:
        day, time, all_day = _when(f["DTSTART"][0], f["DTSTART"][1], tz)
        end = _when(f["DTEND"][0], f["DTEND"][1], tz)[1] if "DTEND" in f and time else None
    except ValueError:
        return None
    if end and end <= (time or ""):
        end = None  # ends after midnight: the planner shows the start only
    uid = (f.get("UID", ("",))[0].strip() or f"{title}|{day}|{time}")[:200]
    rule = dict(p.split("=", 1) for p in f.get("RRULE", ("",))[0].upper().split(";") if "=" in p)
    freq, horizon = rule.get("FREQ"), today + dt.timedelta(days=AHEAD_DAYS)
    if freq == "YEARLY":
        nxt = _next_yearly(day, today)
        return Event(uid, title, nxt.isoformat(), time, end, repeat="year", all_day=all_day)
    if _ended(rule, today, tz):
        return None
    if freq == "WEEKLY" and time and "," not in rule.get("BYDAY", ""):
        return Event(uid, title, None, time, end, weekday=day.weekday())
    if freq in ("DAILY", "WEEKLY", "MONTHLY"):
        day = _next_occurrence(day, freq, int(rule.get("INTERVAL", "1") or 1), today)
    if day < today or day > horizon:
        return None
    return Event(uid, title, day.isoformat(), time, end, all_day=all_day)


def _ended(rule: dict, today: dt.date, tz: ZoneInfo) -> bool:
    until = rule.get("UNTIL")
    if not until:
        return False
    try:
        return _when(until, {}, tz)[0] < today
    except ValueError:
        return False


def _next_yearly(day: dt.date, today: dt.date) -> dt.date:
    for year in range(today.year, today.year + 5):  # 29 February waits for a leap year
        try:
            d = day.replace(year=year)
        except ValueError:
            continue
        if d >= today:
            return d
    return day


def _next_occurrence(day: dt.date, freq: str, step: int, today: dt.date) -> dt.date:
    """The first time on or after today: one plan in the list, not a copy for every week."""
    step = max(step, 1)
    if day >= today:
        return day
    if freq in ("DAILY", "WEEKLY"):
        span = step * (1 if freq == "DAILY" else 7)
        return day + dt.timedelta(days=-(-(today - day).days // span) * span)
    months = (today.year - day.year) * 12 + today.month - day.month
    months = -(-months // step) * step
    for extra in range(0, 13 * step, step):
        m = months + extra
        y, mo = day.year + (day.month - 1 + m) // 12, (day.month - 1 + m) % 12 + 1
        try:
            d = day.replace(year=y, month=mo)
        except ValueError:
            continue
        if d >= today:
            return d
    return day


def link_ok(url: str) -> str | None:
    """A calendar link that is safe to fetch: https (webcal is the same), a public host. Returns the https URL."""
    url = url.strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[9:]
    u = urlparse(url)
    if u.scheme != "https" or not u.hostname or u.port not in (None, 443) or u.username:
        return None
    try:
        addrs = {a[4][0] for a in socket.getaddrinfo(u.hostname, 443)}
    except OSError:
        return None
    for a in addrs:
        ip = ipaddress.ip_address(a.split("%")[0])
        if not ip.is_global:  # no peeking into the server's own network
            return None
    return url
