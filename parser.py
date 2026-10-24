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
              "маникюр", "йога", "пилатес", "девичник", "мальчишник", "с друзьями", "с подругами", "занят")
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
    weekly: int | None = None  # 0..6 for repeating busy days

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


def find_time(t: str) -> str | None:
    # «18:30» is a time; «05.11» is a date (find_date), unless it reads «в 18.30» and can't be a month.
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", t) or re.search(r"\bв (\d{1,2})\.(\d{2})\b(?!\.)", t)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60 and (":" in m.group(0) or int(m.group(2)) > 12):
        return f"{int(m.group(1)):02d}:{m.group(2)}"
    m = re.search(r"\bв (\d{1,2})(?![.:]\d)(?: час\w*)?(?: (утра|дня|вечера|ночи))?\b", t)
    if m:
        h = int(m.group(1))
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


def find_weekly(t: str) -> int | None:
    if re.search(r"кажд\w+|по (понедельникам|вторникам|средам|четвергам|пятницам|субботам|воскресеньям)", t):
        return find_weekday(t.replace("по ", " "))
    return None


# ---------- is it something to write down at all? ----------

# Small talk that must never become a task (and never ping the partner).
CHAT = re.compile(
    r"(привет\w*|здравствуй\w*|здорово|хай|хеллоу|ку|йо|доброе утро|добрый (?:день|вечер)|доброй ночи|спокойной ночи|"
    r"спасибо\w*|спс|благодарю|пасиб\w*|ок|окей|ok|ага|угу|да|нет|неа|хорошо|ладно|понял\w*|принято|ясно|супер|класс|круто|"
    r"пока|до встречи|целую|люблю\w*(?: тебя)?|я тебя люблю|как дела|как ты|ты тут|ты здесь|что делаешь|чем занят\w*|"
    r"кто ты|что ты умеешь|помощь|тест|проверка)"
    r"(?:[ ,]+(?:тебе|вам|бот|большое|огромное|милый|милая|дорогой|дорогая|солнце|зай\w*|а ты|у тебя|как))*"
)
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
    r"кажд\w+ \w+|в \d{1,2}(?:[:.]\d{2})?(?: час\w*)?(?: (?:утра|дня|вечера|ночи))?|\d{1,2}[.:]\d{2}(?:\.\d{2,4})?)\b",
    re.I,
)


def _clean(text: str) -> str:
    out = _JUNK.sub(" ", text)
    out = re.sub(r"\s+", " ", out).strip(" ,.!?-—")
    return out[:1].upper() + out[1:] if out else text.strip()


def rules(text: str, today: dt.date) -> list[Parsed]:
    t = " " + text.lower().replace("ё", "е") + " "
    m = re.match(r"\s*(?:купи(?:ть)?|надо купить|нужно купить|докупить)\b[:,]?\s*(.+)", text, re.I)
    if m or t.strip().startswith("купить"):
        body = m.group(1) if m else text
        when = find_date(t, today)
        parts = [_clean(p) for p in re.split(r",|\sи\s|;|\n", body) if _clean(p).strip(" .")]
        return [Parsed("buy", p, date=when) for p in parts if p]
    weekly = find_weekly(t)
    when = None if weekly is not None else find_date(t, today)
    at = find_time(t)
    if any(w in t for w in BUSY_WORDS):
        return [Parsed("busy", _clean(text), "me", when, at, weekly)]
    if any(w in t for w in EVENT_WORDS):
        return [Parsed("event", _clean(text), "both", when, at)]
    who = "partner" if any(w in t for w in PARTNER_WORDS) else "me"
    if re.match(r"\s*давай(?:те)?\b", t):  # «давай завтра купим продукты» — the two of you
        who = "both"
    return [Parsed("task", _clean(text), who, when, at)]


# ---------- the model ----------

PROMPT = """Ты разбираешь фразы для семейного планера пары. Сегодня {today} ({weekday}).
Фразу написал(а) {author}, партнёр — {partner}.{forwarded}
Верни только JSON: {{"items": [{{"kind": "...", "text": "...", "who": "...", "date": null, "time": null, "weekly": null}}]}}
kind: "task" — дело или обещание (вкрутить лампочку, позвонить маме); "buy" — покупка, по одной позиции на item;
"busy" — личные планы одного из партнёров, когда он занят (футбол, баня, маникюр, работа допоздна);
"event" — совместные планы пары (в гости к маме, кино, годовщина).
text: коротко, с большой буквы, без дат и слов «напомни», «не забудь». Пример: «Вкрутить лампочку в ванной».
who: "me" — делает автор, "partner" — делает партнёр (если фразу обращают к партнёру, «ты обещал», «пусть он»), "both" — оба.
date: "YYYY-MM-DD" или null. «В субботу» — ближайшая будущая суббота. time: "HH:MM" или null.
weekly: номер дня недели 0–6 (0 = понедельник), если повторяется каждую неделю («по субботам баня»), иначе null.
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
    said_time = not source or re.search(r"\d|утр|вечер|дн[её]м|ноч|полдень|полноч|обед", source.lower())
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
        time = time if said_time and isinstance(time, str) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", time) else None
        weekly = it.get("weekly")
        weekly = weekly if isinstance(weekly, int) and 0 <= weekly <= 6 and kind == "busy" else None
        if weekly is not None:
            date = None
        out.append(Parsed(kind, text[:1].upper() + text[1:], who, date, time, weekly))
    return out


def parse(text: str, today: dt.date, author: str = "я", partner: str = "партнёр", forwarded: str = "",
          allow_model: bool = True) -> list[Parsed]:
    """Blocking when the model is on: async callers go through planner.add_text_async.
    forwarded: the partner's name when the message is the partner's own words forwarded by the author."""
    text = strip_greeting(text.strip()[:1000])
    if not text or intent(text, today) != "do":
        return []
    if config.ai_enabled and allow_model:
        fwd = f"\nЭто пересланное сообщение от {forwarded}: «я» в нём — {forwarded}." if forwarded else ""
        system = PROMPT.format(today=today.isoformat(), weekday=WD_FULL[today.weekday()],
                               author=author, partner=partner, forwarded=fwd)
        try:
            items = from_model(_yandex(system, text), today, text)
            if not items:  # a valid empty answer: the model saw small talk, not an errand
                log.info("model found nothing to write down")
            return items
        except Exception as e:  # network, quota, bad JSON: the rules still give a usable answer
            log.warning("model parse failed: %s", type(e).__name__)
    items = rules(text, today)
    if forwarded:  # a forwarded «я сделаю» was written by the partner
        for it in items:
            it.who = {"me": "partner", "partner": "me"}.get(it.who, it.who)
    return items


def human_date(day: str | None, time: str | None = None, today: dt.date | None = None) -> str:
    if not day:
        return f"в {time}" if time else ""
    d = dt.date.fromisoformat(day)
    today = today or dt.date.today()
    delta = (d - today).days
    label = {0: "сегодня", 1: "завтра", 2: "послезавтра", -1: "вчера"}.get(delta)
    if label is None:
        label = f"{WD_SHORT[d.weekday()]} {d:%d.%m}"
    return f"{label} в {time}" if time else label
