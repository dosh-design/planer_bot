"""Phrase → items. «Вкрути лампочку в ванной до субботы» → task for the partner, due Saturday.

The text model does the reading when Yandex keys are set; its JSON is checked and cleaned by code
(kinds, dates, times), so a bad answer can't put garbage into the list. Without keys, or when the
model fails, simple rules handle the common phrases: dates (завтра, в пятницу, 25.10), times,
«купить …», «каждую субботу футбол».
"""
import datetime as dt
import json
import logging
import re
from dataclasses import asdict, dataclass

import names
from config import config

log = logging.getLogger(__name__)

WEEKDAYS = {
    "понедельник": 0, "вторник": 1, "сред": 2, "четверг": 3, "пятниц": 4, "суббот": 5, "воскресен": 6,
    "пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "вс": 6,
}
WD_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
WD_FULL = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
MONTHS = {"январ": 1, "феврал": 2, "март": 3, "апрел": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7,
          "август": 8, "сентябр": 9, "октябр": 10, "ноябр": 11, "декабр": 12}
BUSY_WORDS = ("футбол", "баня", "бане", "баню", "рыбалк", "тренировк", "зал", "хоккей", "покер", "гараж",
              "маникюр", "йога", "пилатес", "девичник", "мальчишник", "с друзьями", "с подругами", "занят",
              "пробежк", "бассейн", "спортзал")
EVENT_WORDS = ("вместе", "мы ", "нам ", "к маме", "к родител", "в гости", "кино", "ресторан", "театр",
               "годовщин", "свидани", "день рождения", "др ")
PARTNER_WORDS = ("ты ", "тебе", "твой", "твоя", "обещал", "не забудь", "напомни ему", "напомни ей", "можешь", "сможешь")


@dataclass
class Parsed:
    kind: str                 # task | buy | busy | event
    text: str
    who: str = "me"           # me | partner | both
    date: str | None = None   # YYYY-MM-DD
    time: str | None = None   # HH:MM
    end: str | None = None    # HH:MM, when the phrase gives a span: «с 8 до 10»
    weekly: int | None = None  # 0..6 for repeating busy days and house chores
    repeat: str | None = None  # "month:21" — a payment on the 21st; "year" — a birthday on its date
    project: str | None = None  # «Ремонт кухни» for the steps of one bigger thing

    def to_dict(self) -> dict:
        return asdict(self)


# ---------- dates and times ----------

def next_weekday(today: dt.date, wd: int) -> dt.date:
    """«В субботу» said on Saturday means the next one; said on Thursday — the day after tomorrow."""
    ahead = (wd - today.weekday()) % 7 or 7
    return today + dt.timedelta(days=ahead)


def find_weekday(t: str) -> int | None:
    for word in re.findall(r"[а-яё]+", t):
        for stem, wd in WEEKDAYS.items():
            if (len(stem) == 2 and word == stem) or (len(stem) > 2 and word.startswith(stem)):
                return wd
    return None


def find_date(t: str, today: dt.date) -> str | None:
    if "послезавтра" in t:
        return (today + dt.timedelta(days=2)).isoformat()
    if "завтра" in t:
        return (today + dt.timedelta(days=1)).isoformat()
    if "сегодня" in t or "вечером" in t and "завтра" not in t:
        return today.isoformat()
    m = re.search(r"через (\d+|день|неделю|две недели)\s*(дн|день|недел)?", t)
    if m:
        n = {"день": 1, "неделю": 7, "две недели": 14}.get(m.group(1))
        if n is None:
            n = int(m.group(1)) * (7 if (m.group(2) or "").startswith("недел") else 1)
        return (today + dt.timedelta(days=n)).isoformat()
    m = re.search(r"(?<![\d.:])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\d:])", t)
    if m and int(m.group(2)) <= 12:
        return _date(today, int(m.group(1)), int(m.group(2)), m.group(3))
    m = re.search(r"\b(\d{1,2}) ([а-я]+)", t)
    if m:
        month = next((n for stem, n in MONTHS.items() if m.group(2).startswith(stem)), None)
        if month:
            return _date(today, int(m.group(1)), month, None)
    if "выходн" in t:
        return next_weekday(today, 5).isoformat() if today.weekday() != 5 else today.isoformat()
    if "на неделе" in t or "до конца недели" in t:
        return (today + dt.timedelta(days=6 - today.weekday())).isoformat()
    wd = find_weekday(t)
    if wd is not None:
        return next_weekday(today, wd).isoformat()
    return None


def _date(today: dt.date, d: int, m: int, y: str | None) -> str | None:
    year = int(y) + (2000 if y and len(y) == 2 else 0) if y else today.year
    try:
        out = dt.date(year, m, d)
    except ValueError:
        return None
    if not y and out < today:  # «5.01» said in October is next January
        out = out.replace(year=year + 1)
    return out.isoformat()


def bad_when(text: str) -> str | None:
    """A date or time the phrase names but that does not exist: «31.02», «30 февраля», «в 25:00».
    The item is still saved without it; the caller says so instead of silently dropping it."""
    t = text.lower().replace("ё", "е")
    for m in re.finditer(r"(?<![\d.:])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\d:])", t):
        d, mo, y = int(m.group(1)), int(m.group(2)), m.group(3)
        year = int(y) + (2000 if len(y) == 2 else 0) if y else 2028  # a leap year: «29.02» without a year is fine
        if mo <= 12 or d <= 12:
            try:
                dt.date(year, mo, d)
            except ValueError:
                if not (mo > 12 and d <= 12):  # «10.15» reads as a time, not a date
                    return m.group(0)
    for m in re.finditer(r"\b(\d{1,2}) ([а-я]+)", t):
        month = next((n for stem, n in MONTHS.items() if m.group(2).startswith(stem)), None)
        if month:
            try:
                dt.date(2028, month, int(m.group(1)))
            except ValueError:
                return m.group(0)
    m = re.search(r"\b(?:в|к|до|с) (\d{1,2}):(\d{2})\b", t)
    if m and (int(m.group(1)) > 23 or int(m.group(2)) > 59):
        return m.group(0)
    return None


# «с восьми до десяти», «в семь»: hours said in words, in the forms that follow «с», «до» and «в».
HOUR_WORDS = {
    "одного": 1, "часу": 1, "час": 1, "двух": 2, "два": 2, "трех": 3, "трёх": 3, "три": 3, "четырех": 4, "четырёх": 4, "четыре": 4,
    "пяти": 5, "пять": 5, "шести": 6, "шесть": 6, "семи": 7, "семь": 7, "восьми": 8, "восемь": 8,
    "девяти": 9, "девять": 9, "десяти": 10, "десять": 10, "одиннадцати": 11, "одиннадцать": 11,
    "двенадцати": 12, "двенадцать": 12, "полудня": 12, "полдень": 12,
}
_HW = "|".join(sorted(HOUR_WORDS, key=len, reverse=True))
_CLOCK = rf"(\d{{1,2}}(?:[:.]\d{{2}})?|{_HW})"
_PART = r"(?: час\w*)?(?: (утра|дня|вечера|ночи))?"
_SPAN = re.compile(rf"\bс {_CLOCK}{_PART}(?: и)? до {_CLOCK}{_PART}(?![\w.:])")
# «8–10 утра», «в 19:00-21:00»: a dash span needs «с»/«в» before it, a clock or a part of day, so «2-3 яблока» stays
_DASH = re.compile(rf"(?:\b([св]) )?\b(\d{{1,2}}(?::\d{{2}})?) ?[-–—] ?(\d{{1,2}}(?::\d{{2}})?)(?: час\w*)?(?: (утра|дня|вечера|ночи))?(?![\w.:])")


def _clock(s: str) -> tuple[int, int] | None:
    if s in HOUR_WORDS:
        return HOUR_WORDS[s], 0
    h, _, m = s.replace(".", ":").partition(":")
    h, m = int(h), int(m or 0)
    return (h, m) if h < 24 and m < 60 else None


def find_span(t: str) -> tuple[str, str] | None:
    """«с 8 до 10 утра» → ("08:00", "10:00"); «с семи до девяти вечера» → ("19:00", "21:00").
    A part of day said once at the end covers both ends; «с 10 до 2» runs past noon; with no part of day
    an afternoon-sized span («с 3 до 5») is read as the afternoon, like a single «в 3»."""
    m = _SPAN.search(t)
    if m:
        a, pa, b, pb = m.groups()
    else:
        m = _DASH.search(t)
        if not m or not (m.group(1) or m.group(4) or ":" in m.group(2) + m.group(3)):
            return None
        a, b, pb = m.group(2), m.group(3), m.group(4)
        pa = None
    a, b = _clock(a), _clock(b)
    if not a or not b:
        return None
    (ha, ma), (hb, mb) = a, b
    if pb in ("дня", "вечера") and hb < 12:
        hb += 12
    if pa in ("дня", "вечера") and ha < 12:
        ha += 12
    elif pa is None and pb in ("дня", "вечера") and ha < 12 and (ha + 12, ma) < (hb, mb):
        ha += 12  # «с 7 до 9 вечера»; but «с 11 до 2 дня» starts at 11
    if not pa and not pb and 1 <= ha <= 7 and 1 <= hb <= 7 and (ha, ma) < (hb, mb):
        ha, hb = ha + 12, hb + 12
    if (hb, mb) <= (ha, ma) and hb + 12 < 24:
        hb += 12  # «с 10 до 2»
    if (hb, mb) <= (ha, ma) or hb > 23:
        return None
    return f"{ha:02d}:{ma:02d}", f"{hb:02d}:{mb:02d}"


def find_time(t: str) -> str | None:
    # «18:30» is a time; «05.11» is a date (find_date), unless it reads «в 18.30» and can't be a month.
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t) or re.search(r"\bв (\d{1,2})\.(\d{2})\b(?!\.)", t)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60 and (":" in m.group(0) or int(m.group(2)) > 12):
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(rf"\bв (\d{{1,2}}|{_HW})(?![.:]\d)(?: час\w*)?(?: (утра|дня|вечера|ночи))?\b", t)
    if m:
        h = HOUR_WORDS.get(m.group(1)) or int(m.group(1))
        part = m.group(2)
        if part in ("дня", "вечера") and h < 12:
            h += 12
        elif not part and 1 <= h <= 7:  # «в 7» about chores means the evening
            h += 12
        if h < 24:
            return f"{h:02d}:00"
    if "утром" in t:
        return "09:00"
    if "вечером" in t:
        return "19:00"
    return None


YEARLY_WORDS = ("день рождения", "днюх", "др ", "годовщин", "именин", "юбиле", "каждый год", "ежегодн")
_MONTHLY = re.compile(r"кажд\w+ месяц|ежемесячн|раз в месяц")
_MONTHDAY = re.compile(r"\b(\d{1,2})(?:-?го|-?е)?(?: числа)?(?= (?:кажд\w+ месяц|ежемесячн|раз в месяц))"
                       r"|(?:кажд\w+ месяц\w*|ежемесячно|раз в месяц),? (\d{1,2})(?:-?го|-?е)?\b(?![.:]\d)"
                       r"|\b(\d{1,2})(?:-?го)? числа\b")


def month_day(year: int, month: int, day: int) -> dt.date:
    """The 31st in a 30-day month is its last day."""
    last = (dt.date(year + month // 12, month % 12 + 1, 1) - dt.timedelta(days=1)).day
    return dt.date(year, month, min(day, last))


def next_month_day(after: dt.date, anchor: int, include: bool = True) -> dt.date:
    """The first «anchor-th of the month» on or after (or strictly after) a date."""
    d = month_day(after.year, after.month, anchor)
    if d < after or (d == after and not include):
        y, m = (after.year + 1, 1) if after.month == 12 else (after.year, after.month + 1)
        d = month_day(y, m, anchor)
    return d


def next_repeat(day: str, repeat: str) -> str:
    """The next date of a repeating item after this one: a day, a week, a month or a year on."""
    d = dt.date.fromisoformat(day)
    if repeat in ("day", "week"):
        return (d + dt.timedelta(days=1 if repeat == "day" else 7)).isoformat()
    if repeat.startswith("month:"):
        return next_month_day(d, int(repeat[6:]), include=False).isoformat()
    try:
        return d.replace(year=d.year + 1).isoformat()
    except ValueError:  # 29 February
        return dt.date(d.year + 1, 2, 28).isoformat()


def prev_repeat(day: str, repeat: str) -> str:
    """The date before this one: undoing a payment ticked by mistake puts it back."""
    d = dt.date.fromisoformat(day)
    if repeat in ("day", "week"):
        return (d - dt.timedelta(days=1 if repeat == "day" else 7)).isoformat()
    if repeat.startswith("month:"):
        y, m = (d.year - 1, 12) if d.month == 1 else (d.year, d.month - 1)
        return month_day(y, m, int(repeat[6:])).isoformat()
    try:
        return d.replace(year=d.year - 1).isoformat()
    except ValueError:
        return dt.date(d.year - 1, 2, 28).isoformat()


def find_repeat(t: str, today: dt.date, when: str | None) -> tuple[str | None, str | None]:
    """(repeat, date): «21-го числа каждого месяца» → ("month:21", the next 21st);
    «день рождения мамы 1 февраля» → ("year", the next 1 February). A birthday without a date is a plain plan."""
    if _MONTHLY.search(t):
        m = _MONTHDAY.search(t)
        anchor = int(next(g for g in m.groups() if g)) if m else dt.date.fromisoformat(when or today.isoformat()).day
        if 1 <= anchor <= 31:
            return f"month:{anchor}", next_month_day(today, anchor).isoformat()
    if when and any(w in t for w in YEARLY_WORDS):
        return "year", when
    return None, when


# «Ремонт кухни: купить плитку, вызвать мастера» — the steps of one bigger thing.
_PROJECT = re.compile(r"\s*([А-ЯЁа-яёA-Za-z][^:\d\n]{1,38}?)\s*:\s*(.+)", re.S)
_NOT_PROJECT = ("купи", "надо купить", "нужно купить", "докупить", "напомни", "важно", "срочно", "кстати", "смотри", "слушай", "итак", "внимание", "ps", "upd")


def split_project(text: str) -> tuple[str, list[str]] | None:
    m = _PROJECT.match(text)
    if not m or m.group(1).strip().lower() in _NOT_PROJECT or m.group(1).strip().lower().startswith(_NOT_PROJECT):
        return None
    steps = [s.strip(" .;") for s in re.split(r",|;|\n|\s+и\s+(?=[а-яё]+(?:ть|ти|чь)\b)", m.group(2)) if s.strip(" .;")]
    name = m.group(1).strip()
    return (name[:1].upper() + name[1:60], steps) if steps else None


def find_weekly(t: str) -> int | None:
    if re.search(r"кажд\w+|по (понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям)", t):
        return find_weekday(t.replace("по ", " "))
    return None


# ---------- is it something to write down at all? ----------

# Small talk that must never become a task (and never ping the partner).
_CHAT_UNIT = (
    r"(?:привет\w*|здравствуй\w*|здорово|хай|хеллоу|ку|йо|доброе утро|добрый (?:день|вечер)|доброй ночи|спокойной ночи|"
    r"спасибо\w*|спс|благодарю|пасиб\w*|ок|окей|ok|ага|угу|да|нет|неа|хорошо|ладно|понял\w*|принято|ясно|супер|класс|круто|"
    r"пока|до встречи|целую|люблю\w*(?: тебя)?|я тебя люблю|как дела|как ты|ты тут|ты здесь|что делаешь|чем занят\w*|"
    r"кто ты|что ты умеешь|помощь|тест|проверка)"
    r"(?:[ ,]+(?:тебе|вам|бот|большое|огромное|милый|милая|дорогой|дорогая|солнце|зай\w*|а ты|у тебя|как))*"
)
# One or several pleasantries in a row: «хорошо, спасибо», «ок, пока», «привет, как дела».
CHAT = re.compile(rf"{_CHAT_UNIT}(?:[ ,]+{_CHAT_UNIT})*")
# «Привет! Купи хлеба» — the greeting goes, the errand stays.
_LEAD = re.compile(r"^\s*(?:привет\w*|здравствуй\w*|доброе утро|добрый (?:день|вечер)|хай|слушай|кстати)\s*[!,.)]*\s+(?=\S)", re.I)


def strip_greeting(text: str) -> str:
    return _LEAD.sub("", text, count=1)


# Words that make a phrase an errand even without a date: an ask, a promise, a «need to».
ACTION_WORDS = ("напомни", "не забудь", "надо", "нужно", "давай", "обещал", "можешь", "сможешь", "пора", "сделай",
                "купи", "закажи", "запиши", "позвони", "забери", "вынеси", "помой", "оплати", "сходи", "приготовь",
                "почини", "повесь", "убери", "отвези", "проверь", "принеси", "выброси", "полей", "покорми", "постирай")
_VERB = re.compile(r"\b[а-яё]{2,}(?:ть|ти|чь)(?:ся)?\b")  # an infinitive: «вкрутить», «отвезти», «испечь»


def intent(text: str, today: dt.date | None = None) -> str:
    """"chat" — small talk, not an item; "unclear" — too little to tell; "do" — parse it into items.

    Phrases of three words and more keep the old behaviour (they become a task), so real errands written
    loosely are not blocked; only greetings, thanks, bare questions and one- or two-word fragments are held back.
    """
    today = today or dt.date.today()
    t = re.sub(r"[^\w\s:.,?-]", " ", strip_greeting(text).lower().replace("ё", "е"))
    words = re.findall(r"[а-яa-z0-9]+", t)
    if not words:
        return "chat"
    bare = " ".join(re.findall(r"[а-яa-z0-9]+", t))
    if CHAT.fullmatch(bare):
        return "chat"
    padded = f" {bare} "
    if len(words) == 1:  # «давай», «купи», «лампочка»: say more
        return "do" if _VERB.search(t) else "unclear"
    strong = (re.match(r"\s*(?:купи|докупи|надо купить|нужно купить)", t) or _VERB.search(t)
              or any(re.search(rf"\b{w}\b", bare) for w in ACTION_WORDS)
              or find_time(t) or find_weekly(t) is not None or find_date(t, today))
    if t.rstrip().endswith("?"):
        return "do" if strong else "chat"
    if strong or any(w in padded for w in BUSY_WORDS + EVENT_WORDS):
        return "do"
    return "do" if len(words) >= 3 else "unclear"


# ---------- rules ----------

_JUNK = re.compile(
    r"\b(давай(?:те)?|(?:с)?можешь(?: ли)?|ты же обещал\w*|ты обещал\w*|обещал\w*|у меня|у нас|пусть|не забудь|напомни(?: ему| ей| мне)?|пожалуйста|плиз|надо|нужно|сегодня|послезавтра|завтра|"
    r"утром|вечером|до выходных|на выходных|в выходные|до конца недели|на неделе|"
    r"через \d+ (?:дн\w+|день|недел\w+)|через (?:день|неделю|две недели)|"
    r"(?:в|во|до|к|по) (?:понедельник\w*|вторник\w*|сред\w+|четверг\w*|пятниц\w+|суббот\w+|воскресень\w+)|"
    r"\d{1,2}(?:-?го|-?е)? числа|\d{1,2}-?го|кажд\w+ месяц\w*|ежемесячно|раз в месяц|каждый год|ежегодно|"
    r"(?:(?:до|к) )?\d{1,2} (?:январ|феврал|март|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*|"
    r"на (?:тебе|мне|нас)|кажд\w+ \w+|по (?:понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям)|в (?:\d{1,2}(?:[:.]\d{2})?|" + _HW + r")(?: час\w*)?(?: (?:утра|дня|вечера|ночи))?|(?:(?:до|к) )?\d{1,2}[.:]\d{2}(?:\.\d{2,4})?)\b",
    re.I,
)


def _clean(text: str) -> str:
    out = re.sub(_SPAN.pattern, " ", text, flags=re.I)
    out = re.sub(_DASH.pattern, lambda m: " " if (m.group(1) or m.group(4) or ":" in m.group(2) + m.group(3)) else m.group(0),
                 out, flags=re.I)
    out = _JUNK.sub(" ", out)
    out = re.sub(r"\s+", " ", out).strip(" ,.!?-—:")
    return out[:1].upper() + out[1:] if out else text.strip()


def _partner(t: str) -> bool:
    """Said to the partner: «ты», «тебе», «обещал» as words, so «выбрать даты» isn't «ты»."""
    return any(re.search(rf"(?<![а-яё]){re.escape(w)}", t) for w in PARTNER_WORDS)


def rules(text: str, today: dt.date, in_project: bool = False) -> list[Parsed]:
    t = " " + text.lower().replace("ё", "е") + " "
    proj = None if in_project else split_project(text)
    if proj and not re.match(r"\s*(?:купи(?:ть)?|надо купить|нужно купить|докупить)\s*:", text, re.I):
        name, steps = proj
        out = []
        for step in steps:
            for it in rules(step, today, in_project=True):
                if it.kind == "task":
                    it.project = name
                out.append(it)
        return out
    m = None if in_project else re.match(r"\s*(?:купи(?:ть)?|надо купить|нужно купить|докупить)\b[:,]?\s*(.+)", text, re.I)
    if m or (t.strip().startswith("купить") and not in_project):
        body = m.group(1) if m else text
        when = find_date(t, today)
        parts = [_clean(p) for p in re.split(r",|\sи\s|;|\n", body) if _clean(p).strip(" .")]
        return [Parsed("buy", p, date=when) for p in parts if p]
    weekly = find_weekly(t)
    when = None if weekly is not None else find_date(t, today)
    at, end = find_span(t) or (find_time(t), None)
    repeat, when = find_repeat(t, today, when) if weekly is None else (None, when)
    if repeat == "year":
        return [Parsed("event", _clean(text), "both", when, at, end, repeat=repeat)]
    if repeat:  # monthly: a payment or a meter reading, the author's unless said otherwise
        who = "partner" if _partner(t) else "me"
        return [Parsed("task", _clean(text), who, when, at, end, repeat=repeat)]
    if any(w in t for w in BUSY_WORDS):
        return [Parsed("busy", _clean(text), "me", when, at, end, weekly)]
    if any(w in t for w in EVENT_WORDS):
        return [Parsed("event", _clean(text), "both", when, at, end)]
    who = "partner" if _partner(t) else "me"
    if re.match(r"\s*давай(?:те)?\b", t):  # «давай завтра купим продукты» — the two of you
        who = "both"
    return [Parsed("task", _clean(text), who, when, at, end, weekly)]


# ---------- the model ----------

PROMPT = """Ты разбираешь фразы для семейного планера пары. Сегодня {today} ({weekday}).
Фразу написал(а) {author}, партнёр — {partner}.{forwarded}
Верни только JSON: {{"items": [{{"kind": "...", "text": "...", "who": "...", "date": null, "time": null, "end": null, "weekly": null, "repeat": null, "project": null}}]}}
kind: "task" — дело или обещание (вкрутить лампочку, позвонить маме); "buy" — покупка, по одной позиции на item;
"busy" — личные планы одного из партнёров, когда он занят (футбол, баня, маникюр, работа допоздна);
"event" — совместные планы пары (в гости к маме, кино, годовщина).
text: коротко, с большой буквы, без дат и слов «напомни», «не забудь». Пример: «Вкрутить лампочку в ванной».
who: "me" — делает автор, "partner" — делает партнёр (если фразу обращают к партнёру, «ты обещал», «пусть он»), "both" — оба.
date: "YYYY-MM-DD" или null. «В субботу» — ближайшая будущая суббота. time: "HH:MM" или null.
end: "HH:MM", если сказано, до скольки («с 8 до 10 утра» → time "08:00", end "10:00»), иначе null.
weekly: номер дня недели 0–6 (0 = понедельник), если повторяется каждую неделю («по субботам баня»), иначе null.
repeat: "month" — платёж или дело каждый месяц в одно число («21-го каждого месяца показания счётчика»), date — ближайшая такая дата;
"year" — важная дата каждый год (день рождения, годовщина), kind "event", date — ближайшая такая дата; иначе null.
project: название большого дела, если фраза перечисляет его шаги («Ремонт кухни: купить плитку, вызвать мастера» —
два item "task" с project "Ремонт кухни»), иначе null.
Регулярная домашняя обязанность («по вторникам выношу мусор», «пылесосить каждую субботу — на тебе») — это "task" с weekly.
Не выдумывай дела, которых нет во фразе. Если во фразе нет ни дела, ни покупки, ни планов (приветствие, спасибо,
вопрос, болтовня), верни {{"items": []}}. Не придумывай время: если его нет во фразе, time = null."""


MODEL_TIMEOUT = 8  # seconds per attempt; with one retry a phrase waits at most ~16 s, then the rules answer
_client = None


def _yandex(system: str, user: str) -> str:
    """Blocking call: run it off the event loop (planner.add_text_async does)."""
    global _client
    from openai import OpenAI

    model = config.yandex_text_model
    if _client is None:
        _client = OpenAI(
            api_key=config.yandex_api_key, base_url="https://ai.api.cloud.yandex.net/v1",
            project=config.yandex_folder_id, timeout=MODEL_TIMEOUT, max_retries=1,
            # Family plans are private: ask Yandex AI Studio not to log request and response bodies.
            default_headers={"x-data-logging-enabled": "false"},
        )
    out = _client.chat.completions.create(
        model=model if "://" in model else f"gpt://{config.yandex_folder_id}/{model}",
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        temperature=0.1,
    )
    return out.choices[0].message.content or ""


def from_model(raw: str, today: dt.date, source: str = "") -> list[Parsed]:
    """Clean the model's JSON: unknown kinds, past or malformed dates and bad times are dropped, not trusted.
    A time the phrase doesn't mention at all (no digits, no part of day) is the model's guess and is dropped too."""
    m = re.search(r"\{.*\}", raw, re.S)
    data = json.loads(m.group(0) if m else raw)
    if not isinstance(data, dict) or not isinstance(data.get("items", []), list):
        raise ValueError("unexpected model answer")
    low = source.lower().replace("ё", "е")
    said_time = not source or re.search(rf"\d|утр|вечер|днем|ноч|полдень|полноч|обед|\b(?:{_HW})\b", low)
    span = find_span(f" {low} ") if source else None
    out = []
    for it in data.get("items", [])[:10]:
        kind = it.get("kind") if it.get("kind") in ("task", "buy", "busy", "event") else "task"
        text = str(it.get("text") or "").strip()[:200]
        if not text:
            continue
        who = it.get("who") if it.get("who") in ("me", "partner", "both") else ("both" if kind == "event" else "me")
        date = it.get("date")
        try:
            date = dt.date.fromisoformat(date).isoformat() if date else None
            if date and date < (today - dt.timedelta(days=1)).isoformat():
                date = None
        except (TypeError, ValueError):
            date = None
        time = it.get("time")
        time = time if said_time and _is_hhmm(time) else None
        end = it.get("end")
        end = end if time and _is_hhmm(end) and end > time else None
        if span:  # a span the code can read is not left to the model: it drops or shifts the end too often
            time, end = span
        weekly = it.get("weekly")
        weekly = weekly if isinstance(weekly, int) and 0 <= weekly <= 6 and kind in ("busy", "task") else None
        if weekly is not None:
            date = None
        repeat = it.get("repeat")
        if repeat == "month" and kind == "task" and date and weekly is None:
            repeat = f"month:{int(date[8:])}"
        elif not (repeat == "year" and kind == "event" and date and weekly is None):
            repeat = None
        project = it.get("project")
        project = str(project).strip()[:60] if project and kind == "task" and str(project).strip() else None
        if source and not repeat and weekly is None:  # a «каждого месяца» the model missed: read it as the rules do
            found, found_date = find_repeat(f" {low} ", today, date)
            if (found or "").startswith("month:") and kind == "task" or found == "year" and kind == "event":
                repeat, date = found, found_date
        out.append(Parsed(kind, text[:1].upper() + text[1:], who, date, time, end, weekly, repeat,
                          project[:1].upper() + project[1:] if project else None))
    return out


def _is_hhmm(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value))


# A leading emoji is the couple's own label («🎂 Купить торт»): it is set aside while the phrase is read,
# so «🎂 Купить торт» is still a purchase, and put back in front of what is saved.
_EMOJI = re.compile(r"^\s*((?:[\U0001F000-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\u2300-\u23FF][\uFE0F\u200D\U0001F3FB-\U0001F3FF]*)+)\s*")


def split_emoji(text: str) -> tuple[str, str]:
    m = _EMOJI.match(text)
    return (m.group(1), text[m.end():]) if m and text[m.end():].strip() else ("", text)


def with_emoji(emoji: str, items: list) -> list:
    if emoji:
        for it in items:
            it.text = f"{emoji} {it.text}"
    return items


def parse(text: str, today: dt.date, author: str = "я", partner: str = "партнёр", forwarded: str = "",
          allow_model: bool = True) -> list[Parsed]:
    """Blocking when the model is on: async callers go through planner.add_text_async.
    forwarded: the partner's name when the message is the partner's own words forwarded by the author."""
    emoji, text = split_emoji(text.strip()[:1000])
    text = strip_greeting(text)
    if not text or intent(text, today) != "do":
        return []
    return with_emoji(emoji, _parse(text, today, author, partner, forwarded, allow_model))


def _parse(text: str, today: dt.date, author: str, partner: str, forwarded: str, allow_model: bool) -> list[Parsed]:
    if config.ai_enabled and allow_model:
        fwd = f"\nЭто пересланное сообщение от {forwarded}: «я» в нём — {forwarded}." if forwarded else ""
        system = PROMPT.format(today=today.isoformat(), weekday=WD_FULL[today.weekday()],
                               author=author, partner=partner, forwarded=fwd)
        try:
            items = from_model(_yandex(system, text), today, text)
            sure = _by_rules(text, today, partner, forwarded)
            sure = [it for it in sure if it.date or it.kind == "buy" or _to_partner(text, partner)]
            if not items:  # an empty answer is small talk, unless the rules see a clear errand (a date, a name)
                if sure:
                    log.info("model found nothing, the rules did")
                return sure
            if sure and not any(it.date for it in items) and any(it.date for it in sure):
                return sure  # the model lost the date the phrase plainly has («день рождения сыночка 28.10»)
            return items
        except Exception as e:  # network, quota, bad JSON: the rules still give a usable answer
            log.warning("model parse failed: %s", type(e).__name__)
    return _by_rules(text, today, partner, forwarded)


def _to_partner(text: str, partner: str):
    return names.leading_name(text, partner) if partner else None


def _by_rules(text: str, today: dt.date, partner: str, forwarded: str) -> list[Parsed]:
    items = rules(text, today)
    if m := _to_partner(text, partner):
        # «Женя, поменяй аватарку»: said to the partner by name, so it is the partner's task, without the name
        named = rules(m, today)
        for it in named:
            if it.who == "me":
                it.who = "partner"
        items = named or items
    if forwarded:  # a forwarded «я сделаю» was written by the partner
        for it in items:
            it.who = {"me": "partner", "partner": "me"}.get(it.who, it.who)
    return items


def human_time(time: str | None, end: str | None = None) -> str:
    """«в 18:30», or «08:00–10:00» for a span."""
    if not time:
        return ""
    return f"{time}–{end}" if end else f"в {time}"


def human_date(day: str | None, time: str | None = None, today: dt.date | None = None, end: str | None = None) -> str:
    if not day:
        return human_time(time, end)
    d = dt.date.fromisoformat(day)
    today = today or dt.date.today()
    delta = (d - today).days
    label = {0: "сегодня", 1: "завтра", 2: "послезавтра", -1: "вчера"}.get(delta)
    if label is None:
        label = f"{WD_SHORT[d.weekday()]} {d:%d.%m}"
    return f"{label}, {human_time(time, end)}" if end else (f"{label} в {time}" if time else label)
