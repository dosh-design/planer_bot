from types import SimpleNamespace

import bot
import db


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))

    async def get_me(self):
        return SimpleNamespace(username="obeshal_bot")


def message(uid, name, text, forwarded=None):
    answers = []

    async def answer(text, **kw):
        answers.append((text, kw.get("reply_markup")))

    return SimpleNamespace(from_user=SimpleNamespace(id=uid, first_name=name, username=None), text=text,
                           forward_origin=forwarded, answer=answer, answers=answers)


async def test_start_with_invite_joins_and_greets_partner():
    b = FakeBot()
    m1 = message(1, "Маша", "/start")
    await bot.start(m1, SimpleNamespace(args=None), b)
    assert "t.me/obeshal_bot?start=" in m1.answers[0][0]
    code = m1.answers[0][0].split("?start=")[1].split()[0]
    m2 = message(2, "Дима", f"/start {code}")
    await bot.start(m2, SimpleNamespace(args=code), b)
    assert "в паре с Маша" in m2.answers[0][0]
    assert b.sent[0][0] == 1 and "Дима" in b.sent[0][1]


async def test_text_adds_task_and_pings_partner(couple):
    b = FakeBot()
    m = message(1, "Маша", "Ты обещал вкрутить лампочку в ванной")
    await bot.text(m, b)
    assert m.answers[0][0].startswith("Записал: 🔧 Вкрутить лампочку в ванной · Дима")
    assert b.sent and b.sent[0][0] == 2


async def test_forwarded_promise_is_put_on_its_author(couple):
    b = FakeBot()
    origin = SimpleNamespace(sender_user=SimpleNamespace(id=2, first_name="Дима"))
    m = message(1, "Маша", "я завтра повешу полку", forwarded=origin)
    await bot.text(m, b)
    assert "· Дима · завтра" in m.answers[0][0]


async def test_menu_buttons_are_not_saved_as_tasks(couple):
    m = message(1, "Маша", bot.BTN_TASKS)
    await bot.text(m, FakeBot())
    assert not db.items(couple)


async def test_small_talk_gets_a_hint_not_a_task(couple):
    b = FakeBot()
    for text in ("Привет", "Спасибо", "Как дела?"):
        m = message(1, "Маша", text)
        await bot.text(m, b)
        assert "записываю дела" in m.answers[0][0]
    assert not db.items(couple) and not b.sent  # the partner heard nothing


async def test_fragment_is_asked_about_then_saved_by_button(couple):
    b = FakeBot()
    m = message(1, "Маша", "Молоко")
    await bot.text(m, b)
    assert "это что?" in m.answers[0][0] and not db.items(couple)
    cb_answers = []

    async def cb_answer(text=None, **kw):
        cb_answers.append(text)

    async def edit_reply_markup(**kw):
        pass

    m.edit_reply_markup = edit_reply_markup
    cb = SimpleNamespace(from_user=SimpleNamespace(id=1), data="force:buy", answer=cb_answer, message=m)
    await bot.cb_force(cb, b)
    assert [(r["kind"], r["text"]) for r in db.items(couple)] == [("buy", "Молоко")]
    await bot.cb_force(cb, b)  # a second press saves nothing more
    assert len(db.items(couple)) == 1


async def test_forward_from_a_third_person_stays_with_the_author(couple):
    b = FakeBot()
    origin = SimpleNamespace(sender_user=SimpleNamespace(id=99, first_name="Мама"))
    m = message(1, "Маша", "я завтра повешу полку", forwarded=origin)
    await bot.text(m, b)
    assert "· Маша · завтра" in m.answers[0][0]


async def test_used_or_foreign_links_explain_and_leak_nothing(couple):
    b = FakeBot()
    m = message(3, "Посторонний", "/start nope")
    await bot.start(m, SimpleNamespace(args="nope"), b)
    assert "не подошла" in m.answers[0][0] and "Дима" not in m.answers[0][0] and "Маша" not in m.answers[0][0]
    assert db.partner(3) is None and not b.sent


async def test_send_retries_once_on_telegram_hiccup(monkeypatch):
    from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
    import bot as chat
    monkeypatch.setattr(chat, "RETRY_PAUSE", 0)

    class Flaky:
        def __init__(self, errors):
            self.errors, self.sent, self.calls = list(errors), [], 0

        async def send_message(self, chat_id, text, reply_markup=None):
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)
            self.sent.append(chat_id)

    b = Flaky([TelegramNetworkError(method=None, message="timeout")])
    await chat.send(b, [chat.Out(1, "напоминание"), chat.Out(2, "второе")])
    assert b.sent == [1, 2] and b.calls == 3  # the first one got its second try, the next went on as usual
    b = Flaky([TelegramForbiddenError(method=None, message="blocked")])
    await chat.send(b, [chat.Out(1, "напоминание")])
    assert b.sent == [] and b.calls == 1  # a block is final, no retry
    b = Flaky([TelegramNetworkError(method=None, message="x"), TelegramNetworkError(method=None, message="x")])
    await chat.send(b, [chat.Out(1, "a"), chat.Out(2, "b")])
    assert b.sent == [2] and b.calls == 3  # gave up on the first after two tries, did not get stuck


async def test_solo_hint_comes_once_not_after_every_phrase():
    b = FakeBot()
    for text in ("купить молоко", "позвонить маме", "вынести мусор"):
        await bot.text(message(7, "Аня", text), b)
    hints = [t for _, t in b.sent if "Позвать пару" in t]
    assert len(hints) == 1 and "только твой" in hints[0]


async def test_same_purchase_is_reported_as_already_listed(couple):
    b = FakeBot()
    await bot.text(message(1, "Маша", "купить молоко"), b)
    m = message(2, "Дима", "Купить молоко")
    await bot.text(m, b)
    assert m.answers[0][0].startswith("Уже в списке: 🛒 Молоко")
    assert not [t for chat, t in b.sent if chat == 1 and "Молоко" in t[20:]]  # Маша isn't told about it again


async def test_greeting_has_the_new_name_and_motto():
    m = message(8, "Оля", "/start")
    await bot.start(m, SimpleNamespace(args=None), FakeBot())
    assert "«Договорились»" in m.answers[0][0] and "Меньше бытового хаоса. Больше времени вместе." in m.answers[0][0]
