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
    code = db.couple_code(1)
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
