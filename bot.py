"""Telegram chat: invite the partner, add items by text, voice or a forwarded message, lists, reminders."""
import asyncio
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter, TelegramServerError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton as B,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

import db
import game
import jokes
import parser
import planner
import stt
from config import config
from planner import Out, esc

router = Router()
log = logging.getLogger(__name__)

BTN_TASKS, BTN_BUY, BTN_BUSY, BTN_INVITE = "📋 Дела", "🛒 Покупки", "🚫 Занятые дни", "💌 Позвать пару"
MENU = (BTN_TASKS, BTN_BUY, BTN_BUSY, BTN_INVITE)
_me: dict = {}

HELP = (
    "Пиши или наговаривай голосом как есть, я сам разложу:\n"
    "• «По вторникам выносить мусор — на тебе» → обязанность по дому, каждую неделю\n"
    "• «Вкрути лампочку в ванной до субботы» → дело на партнёра\n"
    "• «Купить хлеб, молоко и батарейки» → покупки\n"
    "• «В воскресенье пробежка с 8 до 10» → твоё занятое время\n"
    "• «В воскресенье едем к маме» → общий план, проверю, что никто не занят\n"
    "• «21-го каждого месяца передать показания» → платёж, после отметки переносится на следующий месяц\n"
    "• «День рождения мамы 1 февраля» → важная дата, напомню за неделю и в сам день\n"
    "• «Ремонт кухни: купить плитку, вызвать мастера» → проект со списком шагов\n"
    "Можно переслать сообщение из вашей переписки: «сделай, пожалуйста…» тоже пойму.\n\n"
    "Каждое сделанное дело — очко семье в общем матче против бытового хаоса: /season.\n"
    "Напоминаю с юмором; тон и частоту можно поменять: /reminders."
)


def kb(rows) -> InlineKeyboardMarkup | None:
    rows = [[B(text=t, callback_data=d) for t, d in r] for r in rows if r]
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def menu() -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BTN_TASKS), KeyboardButton(text=BTN_BUY)],
            [KeyboardButton(text=BTN_BUSY), KeyboardButton(text=BTN_INVITE)]]
    if config.webapp_url:
        rows.append([KeyboardButton(text="📱 Открыть планер", web_app=WebAppInfo(url=f"{config.webapp_url}/tg"))])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Что надо не забыть?")


TRANSIENT = (TelegramRetryAfter, TelegramNetworkError, TelegramServerError)
RETRY_PAUSE = 1.0


async def send(bot: Bot, outs: list[Out]) -> None:
    """Deliver messages one by one. A reminder is marked as sent before it gets here, so a hiccup on
    Telegram's side (flood limit, network, 5xx) gets one more try instead of silently losing it."""
    for o in outs:
        for attempt in (1, 2):
            try:
                await bot.send_message(o.chat_id, o.text, reply_markup=kb(o.buttons))
                break
            except TRANSIENT as e:
                if attempt == 2:
                    log.warning("send to %s failed twice: %s", o.chat_id, type(e).__name__)
                    break
                wait = getattr(e, "retry_after", None) or RETRY_PAUSE
                await asyncio.sleep(min(float(wait), 30))
            except Exception as e:  # the partner blocked the bot or never started it: no point retrying
                log.info("send to %s failed: %s", o.chat_id, type(e).__name__)
                break


async def bot_username(bot: Bot) -> str:
    if "username" not in _me:
        _me["username"] = (await bot.get_me()).username
    return _me["username"]


async def invite_link(bot: Bot, uid: int, fresh: bool = False) -> str:
    """A one-time link (48 h) into the user's couple; "" when the couple is already full."""
    code = db.invite_code(uid, fresh)
    if not code:
        return ""
    if "username" not in _me:
        _me["username"] = (await bot.get_me()).username
    return f"https://t.me/{_me['username']}?start={code}"


INVITE_RULES = "Ссылка одноразовая и работает 48 часов. Не публикуй её: по ней можно попасть в ваш планер."
JOIN_FAIL = {
    "full": "В этой паре уже двое. Планер для двоих, третий тут лишний 🙂",
    "used": "Эта ссылка уже использована. Попроси партнёра прислать новую: /invite.",
    "expired": "Ссылка устарела: приглашения живут 48 часов. Попроси партнёра прислать новую: /invite.",
    "revoked": "Эту ссылку уже заменили новой. Попроси партнёра прислать свежую: /invite.",
    "unknown": "Ссылка не подошла. Попроси партнёра прислать новую: /invite.",
}


def display_name(msg: Message) -> str:
    return (msg.from_user.first_name or msg.from_user.username or "Я").strip()


@router.message(CommandStart())
async def start(msg: Message, command: CommandObject, bot: Bot) -> None:
    uid = msg.from_user.id
    db.save_user(uid, display_name(msg))
    code = (command.args or "").strip()
    if code.startswith("login_"):  # the phone app asked to sign in with this Telegram account
        if db.login_confirm(code[6:], uid):
            await msg.answer("✅ Вход выполнен. Вернитесь в приложение «Договорились», оно откроется само.", reply_markup=menu())
        else:
            await msg.answer("Эта ссылка для входа устарела. Нажмите «Войти через Telegram» в приложении ещё раз.", reply_markup=menu())
        return
    if code:
        result = db.join(uid, code)
        if result == "ok":
            p = db.partner(uid)
            await msg.answer(f"🎉 Готово, вы в паре с {esc(p['name'])}. Теперь всё, что вы обещали сделать, будет записано.\n\n{HELP}",
                             reply_markup=menu())
            await send(bot, [Out(p["id"], f"🎉 {esc(display_name(msg))} теперь в вашем планере. Отговорки кончились.")])
            return
        if result == "taken":
            p = db.partner(uid)
            await msg.answer(f"Ты уже в паре с {esc(p['name'])}, поэтому по чужой ссылке не переключаю. "
                             "Чтобы перейти в другую пару, сначала выйди из этой: /delete.", reply_markup=menu())
            return
        if result in JOIN_FAIL:
            await msg.answer(JOIN_FAIL[result], reply_markup=menu())
            return
    partner = db.partner(uid)
    pair = f"Вы в паре с {esc(partner['name'])}." if partner else (
        "Начать можно и одному, но делить дела удобнее вдвоём. Перешли второй половине эту ссылку:\n"
        f"{await invite_link(bot, uid)}\n{INVITE_RULES}")
    await msg.answer(f"Привет! Я «{esc(config.brand)}», планер для пары. {esc(config.slogan)}\n\nПомогаю делить домашние дела, покупки и планы, чтобы ничего не терялось и никто не тянул всё на себе.\n\n"
                     f"{pair}\n\n{HELP}", reply_markup=menu(), disable_web_page_preview=True)


@router.message(Command("projects"))
async def projects_list(msg: Message) -> None:
    u = _user(msg)
    if not u:
        return
    projects = db.projects(u["couple_id"])
    if not projects:
        await msg.answer("Проектов пока нет. Напиши, например: «Отпуск: купить билеты, оформить страховку», "
                         "или создай проект в мини-приложении.", reply_markup=menu())
        return
    today = db.local_now().date()
    parts = []
    for pr in projects:
        steps = [r for r in db.project_items(pr["id"], u["couple_id"]) if r["status"] == "open"]
        head = f"<b>📁 {esc(pr['name'])}</b> · {pr['done']} из {pr['total']}"
        parts.append("\n".join([head] + [planner.line(r, today) for r in steps[:10]]))
    await msg.answer("\n\n".join(parts), reply_markup=menu())


@router.message(Command("help"))
async def help_(msg: Message) -> None:
    await msg.answer(HELP, reply_markup=menu())


@router.message(Command("invite"))
@router.message(F.text == BTN_INVITE)
async def invite(msg: Message, bot: Bot) -> None:
    db.save_user(msg.from_user.id, display_name(msg))
    p = db.partner(msg.from_user.id)
    if p:
        await msg.answer(f"Вы уже в паре с {esc(p['name'])}. Менять партнёра через бота не рекомендуем 😏")
        return
    link = await invite_link(bot, msg.from_user.id)
    await msg.answer(f"Перешли эту ссылку своей половине:\n{link}\n\n{INVITE_RULES}", disable_web_page_preview=True,
                     reply_markup=kb([[("🔄 Новая ссылка, старую отменить", "newinvite")]]))


@router.callback_query(F.data == "newinvite")
async def cb_newinvite(cb: CallbackQuery, bot: Bot) -> None:
    link = await invite_link(bot, cb.from_user.id, fresh=True)
    await cb.answer("Старая ссылка больше не работает" if link else "Вы уже в паре")
    if link and cb.message:
        await cb.message.answer(f"Новая ссылка:\n{link}\n\n{INVITE_RULES}", disable_web_page_preview=True)


def _section(title: str, rows, today) -> str:
    return f"<b>{title}</b>\n" + "\n".join(planner.line(r, today) for r in rows) if rows else ""


@router.message(Command("list"))
@router.message(F.text == BTN_TASKS)
async def tasks(msg: Message) -> None:
    u = _user(msg)
    if not u:
        return
    db.roll_yearly(db.local_now().date())
    rows = [r for r in db.items(u["couple_id"]) if r["kind"] in ("task", "event")]
    if not rows:
        await msg.answer("Дел нет. Подозрительно. Наговори голосом, что обещали сделать на этой неделе.")
        return
    today = db.local_now().date()
    p = db.partner(u["id"])
    hot = [r for r in rows if planner.is_hot(r, today)]
    hot_ids = {r["id"] for r in hot}
    chores = [r for r in rows if r["weekday"] is not None and r["id"] not in hot_ids]
    in_project = [r for r in rows if r["project_id"] and r["id"] not in hot_ids]
    cold = [r for r in rows if r["id"] not in hot_ids and r["weekday"] is None and not r["project_id"]]
    projects = db.projects(u["couple_id"])
    parts = [
        _section("🔥 ГОРИТ", hot, today),
        _section("Твои", [r for r in cold if r["assignee_id"] == u["id"]], today),
        _section(f"Дела: {esc(p['name'])}" if p else "Партнёра", [r for r in cold if p and r["assignee_id"] == p["id"]], today),
        _section("Общие", [r for r in cold if r["assignee_id"] is None], today),
        _section("🏠 Обязанности по дому", chores, today),
        *[_section(f"📁 {esc(pr['name'])} · {pr['done']} из {pr['total']}",
                   [r for r in in_project if r["project_id"] == pr["id"]], today) for pr in projects],
    ]
    todo = hot + cold + [r for r in chores if not planner.chore_done(r, today)] + in_project
    buttons = [[(f"✅ {r['text'][:28]}", f"done:{r['id']}")] for r in todo[:15]]
    await msg.answer("\n\n".join(x for x in parts if x), reply_markup=kb(buttons))


@router.message(Command("buy"))
@router.message(F.text == BTN_BUY)
async def shopping(msg: Message) -> None:
    u = _user(msg)
    if not u:
        return
    rows = db.items(u["couple_id"], "buy")
    if not rows:
        await msg.answer("Список покупок пуст. Холодильник, видимо, тоже скоро будет.")
        return
    buttons = [[(f"✅ {r['text'][:30]}", f"done:{r['id']}")] for r in rows[:20]]
    await msg.answer("🛒 <b>Купить</b>\n" + "\n".join(f"• {esc(r['text'])}" for r in rows), reply_markup=kb(buttons))


@router.message(Command("busy"))
@router.message(F.text == BTN_BUSY)
async def busy(msg: Message) -> None:
    u = _user(msg)
    if not u:
        return
    rows = db.items(u["couple_id"], "busy")
    if not rows:
        await msg.answer("Занятых дней нет. Напиши, например: «по четвергам у меня футбол» или «в субботу у меня баня».")
        return
    today = db.local_now().date()
    buttons = [[(f"🗑 {r['text'][:30]}", f"del:{r['id']}")] for r in rows[:20]]
    await msg.answer("🚫 <b>Занятые дни</b>\n" + "\n".join(planner.line(r, today) for r in rows), reply_markup=kb(buttons))


@router.message(Command("privacy"))
async def privacy(msg: Message) -> None:
    await msg.answer(
        "Храню только твоё имя из Telegram и записи вашей пары. Их видите только вы двое.\n"
        "Голосовые не сохраняю: распознаю через Yandex SpeechKit и сразу забываю, остаётся только текст дела.\n"
        "Удалить себя и свои данные: /delete"
    )


@router.message(Command("delete"))
async def delete(msg: Message) -> None:
    await msg.answer("Точно удалить тебя из планера? Если партнёр останется, общие записи останутся у него.",
                     reply_markup=kb([[("Да, удалить", "forget"), ("Нет", "keep")]]))


def _user(msg: Message):
    db.save_user(msg.from_user.id, display_name(msg))
    u = db.user(msg.from_user.id)
    if not u["couple_id"]:
        db.couple_code(u["id"])
        u = db.user(u["id"])
    return u


CHAT_REPLY = ("Я записываю дела, покупки и планы, а поболтать лучше друг с другом 🙂\n"
              "Например: «вкрути лампочку до субботы», «купить молоко», «по четвергам у меня футбол».")


async def _reply_added(msg: Message, bot: Bot, text: str, forwarded: str = "") -> None:
    u = _user(msg)
    intent = parser.intent(text, db.local_now().date())
    if intent == "chat":
        await msg.answer(CHAT_REPLY)
        return
    if intent == "unclear":
        # Ask instead of guessing; the phrase waits in kv until a button is pressed.
        db.kv_set(f"pending:{u['id']}", text[:300])
        await msg.answer(f"«{esc(text[:100])}» — это что?", reply_markup=kb([[("📝 Дело", "force:task"), ("🛒 Покупка", "force:buy")],
                                                                             [("Ничего, просто так", "force:no")]]))
        return
    before = db.last_item_id()
    rows, notes = await planner.add_text_async(u["id"], text, forwarded)
    if not rows:
        await msg.answer("Не понял, что записать. Скажи проще: что сделать и когда.")
        return
    await _confirm(msg, u, rows, notes, bot, before)
    if note := planner.bad_when_note(text, rows):
        await msg.answer(note)


async def _confirm(msg: Message, u, rows, notes, bot: Bot, before: int = 0) -> None:
    today = db.local_now().date()
    for r in rows:
        warn = planner.warnings(r)
        text = ("Уже в списке: " if r["id"] <= before else "Записал: ") + planner.line(r, today) + ("\n\n" + "\n".join(warn) if warn else "")
        await msg.answer(text, reply_markup=kb(planner.item_buttons(r)))
    if not db.partner(u["id"]) and not db.kv_get(f"solo_hint:{u['id']}"):
        # Once, not after every phrase: planning alone is allowed, the invite is a suggestion.
        db.kv_set(f"solo_hint:{u['id']}", "1")
        notes.append(Out(u["id"], "💡 Пока планер только твой. Когда захочешь делить дела вдвоём, нажми «💌 Позвать пару»."))
    await send(bot, notes)


@router.message(F.voice)
async def voice(msg: Message, bot: Bot) -> None:
    if not db.user(msg.from_user.id):
        db.save_user(msg.from_user.id, display_name(msg))
    if msg.voice.duration > stt.MAX_SECONDS or msg.voice.file_size and msg.voice.file_size > stt.MAX_BYTES:
        await msg.answer("Длинновато. До 30 секунд, пожалуйста: я планер, а не психолог.")
        return
    if not config.ai_enabled:
        await msg.answer("Голос пока не подключён. Напиши текстом.")
        return
    key = f"voice:{msg.from_user.id}:{db.local_now().date().isoformat()}"
    used = int(db.kv_get(key) or 0)
    if used >= config.voice_daily_limit:
        await msg.answer("На сегодня голосовых хватит, давай текстом. Завтра снова выслушаю.")
        return
    db.kv_set(key, str(used + 1))
    try:
        audio = await bot.download(msg.voice)
        text = await stt.recognize(audio.read())
    except Exception as e:
        log.warning("voice failed: %s", type(e).__name__)
        await msg.answer("Не разобрал голосовое. Попробуй ещё раз или напиши текстом.")
        return
    if not text:
        await msg.answer("Тишина в эфире. Скажи ещё раз, погромче.")
        return
    await msg.answer(f"🎙 Расслышал: «{esc(text)}»")
    await _reply_added(msg, bot, text, _forwarded_name(msg))


def _forwarded_name(msg: Message) -> str:
    """The partner's name when the forwarded message is provably the partner's own (Telegram shows the sender's id).
    A forward from anyone else, or from a hidden account, is treated as the author's own note: «я сделаю» from
    a mother or a colleague must not land on the partner."""
    o = msg.forward_origin
    user = getattr(o, "sender_user", None) if o else None
    p = db.partner(msg.from_user.id)
    if user and p and user.id == p["id"]:
        return p["name"]
    return ""


@router.message(F.text & ~F.text.startswith("/"))
async def text(msg: Message, bot: Bot) -> None:
    if msg.text in MENU:
        return
    await _reply_added(msg, bot, msg.text, _forwarded_name(msg))


@router.callback_query(F.data.startswith("done:"))
async def cb_done(cb: CallbackQuery, bot: Bot) -> None:
    d = planner.complete(cb.from_user.id, int(cb.data.split(":")[1]))
    await cb.answer(d.toast or ("Готово!" if d.ok else "Уже сделано или удалено"))
    if d.ok and cb.message:
        mark = f"✅ Сделано · {esc(d.toast)}" if d.toast else "✅ Сделано"
        try:
            await cb.message.edit_text(cb.message.html_text + "\n\n" + mark, reply_markup=None)
        except Exception:
            pass
    await send(bot, d.notes)


RESULT_NAMES = {"family": "победа семьи", "chaos": "победа хаоса", "draw": "ничья"}


def season_text(couple_id: int) -> str:
    """The same numbers as the mini-app's «Наш сезон»: both read game.season."""
    s = game.season(couple_id, db.local_now().date())
    cur = s["current"]
    lines = ["<b>🏆 Наш сезон: Семья против бытового хаоса</b>",
             f"Неделя {cur['label']}: <b>{game.score_line(cur['family'], cur['chaos'])}</b>", cur["status"]]
    if s["last"]:
        last = s["last"]
        lines.append(f"Прошлый матч ({last['label']}): {last['family']}:{last['chaos']}, {RESULT_NAMES[last['result']]}.")
    got = [a for a in s["achievements"] if a["unlocked"]]
    lines.append("\n<b>Достижения</b>: " + (", ".join(f"{a['icon']} {a['title']}" for a in got) if got
                                         else "пока нет. Первое — за первое сделанное дело."))
    lines.append("\n" + "\n".join(f"• {r}" for r in s["rules"]))
    return "\n".join(lines)


@router.message(Command("season"))
async def season(msg: Message) -> None:
    await msg.answer(season_text(_user(msg)["couple_id"]))


def _modes_kb(current: str):
    return kb([[((("● " if m == current else "") + jokes.NUDGE_MODE_NAMES[m]), f"nmode:{m}")
                for m in jokes.NUDGE_MODES[:2]],
               [((("● " if m == current else "") + jokes.NUDGE_MODE_NAMES[m]), f"nmode:{m}")
                for m in jokes.NUDGE_MODES[2:]]])


MODES_ABOUT = ("Напоминания о просроченных делах:\n"
               "• С юмором — на 2, 4 и 7-й день после срока, дальше всё реже;\n"
               "• Мягко — то же, но без сарказма;\n"
               "• Минимум — только на 2-й и 7-й день;\n"
               "• Выключены — не пишу о просрочке. Напоминания на время, которое вы сами поставили, приходят всегда.")


@router.message(Command("reminders"))
async def reminders(msg: Message) -> None:
    u = _user(msg)
    await msg.answer(MODES_ABOUT, reply_markup=_modes_kb(planner.nudge_mode(u["id"])))


@router.callback_query(F.data.startswith("nmode:"))
async def cb_mode(cb: CallbackQuery) -> None:
    mode = cb.data.split(":")[1]
    if mode not in jokes.NUDGE_MODES or not db.set_nudge_mode(cb.from_user.id, mode):
        await cb.answer("Не получилось")
        return
    await cb.answer(f"Напоминания: {jokes.NUDGE_MODE_NAMES[mode]}")
    if cb.message:
        try:
            await cb.message.edit_reply_markup(reply_markup=_modes_kb(mode))
        except Exception:
            pass


@router.callback_query(F.data.startswith("force:"))
async def cb_force(cb: CallbackQuery, bot: Bot) -> None:
    """The answer to «это что?» for a phrase too short to tell."""
    uid, kind = cb.from_user.id, cb.data.split(":")[1]
    text = db.kv_get(f"pending:{uid}")
    db.kv_set(f"pending:{uid}", "")
    if not text or kind == "no":
        await cb.answer("Ок, ничего не записал")
        if cb.message:
            await cb.message.edit_reply_markup(reply_markup=None)
        return
    u = db.user(uid)
    if not u or not u["couple_id"]:  # left the planner (/delete) while the question was open
        await cb.answer("Сначала /start")
        return
    clean = text.strip()[:200]
    rows, notes = planner.add_parsed(uid, [parser.Parsed(kind, clean[:1].upper() + clean[1:])])
    await cb.answer("Записал")
    if cb.message:
        await cb.message.edit_reply_markup(reply_markup=None)
        await _confirm(cb.message, u, rows, notes, bot)


@router.message(Command("stats"))
async def stats(msg: Message) -> None:
    """Admins only (ADMIN_IDS): the goal metric, couples of two still active on day 14."""
    if msg.from_user.id not in config.admin_ids:
        return
    r, s = db.retention(14), db.stats()
    await msg.answer(f"Пар старше 14 дней: {r['couples']}, из них активны оба за 7 дней: {r['active']}.\n"
                     f"Всего людей: {s['users']}, пар из двух: {s['couples']}, записей: {s['items']}, сделано: {s['done']}.")


@router.callback_query(F.data.startswith("del:"))
async def cb_del(cb: CallbackQuery) -> None:
    ok = planner.remove(cb.from_user.id, int(cb.data.split(":")[1]))
    await cb.answer("Удалил" if ok else "Уже удалено")
    if ok and cb.message:
        try:
            await cb.message.edit_text(cb.message.html_text + "\n\n🗑 Удалено", reply_markup=None)
        except Exception:
            pass


@router.callback_query(F.data.startswith("who:"))
async def cb_who(cb: CallbackQuery, bot: Bot) -> None:
    _, item_id, who = cb.data.split(":")
    uid = cb.from_user.id
    if not planner.reassign(uid, int(item_id), who):
        await cb.answer("Не нашёл запись")
        return
    u = db.user(uid)
    row = db.item(int(item_id), u["couple_id"])
    await cb.answer("Переназначил")
    if not row:
        return
    if cb.message:
        try:
            await cb.message.edit_text("Записал: " + planner.line(row), reply_markup=kb(planner.item_buttons(row)))
        except Exception:
            pass
    await send(bot, planner.assigned_notes(uid, row))


@router.callback_query(F.data == "forget")
async def cb_forget(cb: CallbackQuery) -> None:
    db.delete_user(cb.from_user.id)
    await cb.answer("Удалил")
    if cb.message:
        await cb.message.edit_text("Удалил. Если передумаешь — /start.")


@router.callback_query(F.data == "keep")
async def cb_keep(cb: CallbackQuery) -> None:
    await cb.answer("Оставил всё как есть")
    if cb.message:
        await cb.message.edit_text("Оставил всё как есть.")
