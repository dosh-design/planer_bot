"""Reminder lines. The tone is the product: sarcastic and mocking, never rude, never about looks or money.

{who} is the assignee's name, {what} the task, {days} how long it hangs, {author} the partner who asked.
Overdue lines never name the partner: the couple plays together against the household chaos.
The line is picked by item id and nudge count, so the same nudge always reads the same (and tests stay stable).
"""

REMIND = [
    "⏰ {who}, время пришло: {what}. Делай вид, что вспомнил(а) сам(а).",
    "⏰ Напоминаю, пока бытовой хаос не заметил: {what}.",
    "⏰ {what}. Сейчас. Да, прямо сейчас, а не «щас».",
    "⏰ {who}, у тебя по плану подвиг: {what}. Ордена не будет, но будет спокойный вечер.",
    "⏰ Внимание, {who}: {what}. Этот бот всё помнит. В отличие от некоторых.",
]

NUDGE = [
    "🐌 {who}, «{what}» висит уже {days}. Улитка бы справилась быстрее, но у неё нет дивана.",
    "🕯 «{what}»: день {days_n}. Бытовой хаос пока молчит. Это самое страшное.",
    "📜 Историки уже пишут про «{what}». Обещано {days} назад. {who}, войди в историю как человек, который это сделал.",
    "🔦 {who}, «{what}» всё ещё ждёт. Семья пока справляется фонариком на телефоне.",
    "🏆 Поздравляем, {who}! «{what}» — рекорд ожидания: {days}. Побить его можно только одним способом: сделать.",
    "🧘 «{what}». {days}. Мы не давим. Мы просто будем напоминать. Вечно.",
    "📉 Курс фразы «завтра сделаю» падает уже {days}. Причина: «{what}».",
    "🙃 {who}, «{what}» — это не декоративная задача. Её можно сделать, а не только хранить.",
]

# Overdue nudges by stage: day 2 soft, day 4 friendly sarcasm, day 7 the house motto, then rarer and rarer.
# The partner is never named in them: the family plays together against the household chaos, nobody is to blame.
OVERDUE_SOFT = [
    "🙂 «{what}» всё ещё ждёт своего звёздного часа. Срок был {when}.",
    "🙂 Напоминаем без давления: тут есть одно маленькое дело — «{what}».",
    "🙂 Бытовой хаос интересуется: «{what}» уже в работе или пока в отпуске?",
]

OVERDUE_SARCASM = [
    "😏 «{what}» уже успело обзавестись пропиской: {days} после срока.",
    "😏 Мы не торопим. Просто «{what}» скоро начнёт платить аренду за место в списке.",
    "😏 Кажется, «{what}» решило стать частью интерьера. {days} после срока.",
    "😏 Бытовой хаос просит передать, что он уже начал привыкать к «{what}».",
]

OVERDUE_WEEK = [
    "😎 Пацан сказал — пацан сделал! А «{what}» пока всё ещё ждёт своего часа.",
    "🤝 Слово дано. Теперь осталось красиво выполнить: «{what}».",
    "🤝 Семейный штаб напоминает: «{what}» всё ещё в игре.",
    "🤝 Неделя прошла, а «{what}» всё ещё здесь. Может, сегодня его звёздный час?",
]

OVERDUE_WEEK_SOFT = OVERDUE_WEEK[1:]  # the soft tone keeps the motto out

OVERDUE_LONG = [
    "🗿 «{what}»: {days} после срока. Это уже почти памятник, но его ещё можно красиво снести.",
    "🕰 «{what}» ждёт {days}. Сделать или честно перенести — оба варианта лучше, чем молчать.",
    "📜 «{what}» в списке {days} после срока. Напоминаем всё реже, но не забываем.",
]

OVERDUE_LONG_SOFT = [
    "🕰 «{what}» ждёт {days}. Если планы поменялись, срок можно перенести.",
    "🙂 Тихое напоминание: «{what}» всё ещё в списке. Сделать или перенести — как удобнее.",
]

# The morning digest on a deadline day: one line, not one message per task.
DUE_TODAY = [
    "👀 Кажется, у этих дел сегодня важный день.",
    "🎯 Дедлайн на горизонте. Самое время красиво закрыть вопрос.",
    "🧹 Бытовой хаос уже присматривается к сегодняшним делам.",
]

DUE_TODAY_SOFT = ["🎯 Сегодня срок у этих дел. Всё успеется."]

NUDGE_MODES = ("humor", "soft", "min", "off")
NUDGE_MODE_NAMES = {"humor": "С юмором", "soft": "Мягко", "min": "Минимум", "off": "Выключены"}


def overdue(stage: int, mode: str = "humor") -> list[str] | None:
    """The lines for an overdue stage in this reminder mode; None when this mode skips the stage.
    humor: every stage. soft: every stage, no sarcasm. min: only day 2 and day 7, soft. off: nothing."""
    if mode == "off" or (mode == "min" and stage not in (1, 3)):
        return None
    if mode in ("soft", "min"):
        return (OVERDUE_SOFT, OVERDUE_SOFT, OVERDUE_WEEK_SOFT)[stage - 1] if stage <= 3 else OVERDUE_LONG_SOFT
    return (OVERDUE_SOFT, OVERDUE_SARCASM, OVERDUE_WEEK)[stage - 1] if stage <= 3 else OVERDUE_LONG


# After a task is really done (only when the family's point was counted).
WIN_DONE = [
    "Семья 1:0 — Бытовой хаос! 🎉",
    "Есть! Ещё одна победа над бытовым хаосом.",
    "Задача выполнена. Семейная команда становится сильнее!",
    "Минус одно дело в списке. Плюс спокойствие в доме.",
    "Красиво закрыли вопрос!",
]

# The week's result. {f} and {c} are the family's and the chaos's points.
WEEK_FAMILY = [
    "СЕМЬЯ ПОБЕДИЛА! 🏆 Бытовой хаос снова получил по своим пыльным полкам!",
    "Семья {f}:{c} — Бытовой хаос! Неделя под контролем.",
    "Неделя под контролем. Вы отличная команда! 🏆",
    "Бытовой хаос рассчитывал на большее. Не повезло! 🏆",
]

WEEK_CHAOS = [
    "В этот раз бытовой хаос вырвался вперёд. Но реванш уже близко!",
    "Хаос выиграл раунд, но не войну. Новая неделя — новый матч!",
    "Кажется, у хаоса был слишком сильный состав. Пересобираемся и возвращаемся!",
]

WEEK_DRAW = [
    "Ничья! Хаос сопротивлялся, но и вы ему спуску не дали.",
    "1:1 по настроению, 0:0 по победителю. Продолжаем матч на следующей неделе!",
]


DONE = [
    "✅ {who} сделал(а): {what}. Запишем в летопись подвигов.",
    "✅ «{what}» — готово! {who}, бот снимает шляпу. Немного удивлённо.",
    "✅ Свершилось: {what}. {who}, так можно было?",
]

CONFLICT = (
    "⚠️ Накладка: {day} общий план совпадает с делом «{what}» ({who}). "
    "Планировать на этот день совместное — смелый поступок. Бот предупредил."
)

BUSY_CLASH = (
    "⚠️ {day} у вас уже общий план «{what}». "
    "Ставить на этот день свои дела — всё равно что прийти на свадьбу в шортах. Можно, но обсудите."
)


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        word = one
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        word = few
    else:
        word = many
    return f"{n} {word}"


def plural_days(n: int) -> str:
    return plural(n, "день", "дня", "дней")


def pick(lines: list[str], seed: int) -> str:
    return lines[seed % len(lines)]
