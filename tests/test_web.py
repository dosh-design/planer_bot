import hashlib
import hmac
import json
import time
from urllib.parse import urlencode

import pytest

import db
import web
from config import config


def init_data(uid: int, name: str) -> str:
    fields = {"auth_date": str(int(time.time())), "user": json.dumps({"id": uid, "first_name": name})}
    check = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", config.bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


@pytest.fixture
async def client(aiohttp_client):
    return await aiohttp_client(web.build_app())


async def test_api_needs_telegram_signature(client):
    assert (await client.get("/api/state")).status == 401
    assert (await client.get("/api/state", headers={"X-Init-Data": "user=1&hash=bad"})).status == 401
    assert (await client.get("/tg")).status == 200


async def test_add_list_done_flow(client, couple):
    h = {"X-Init-Data": init_data(1, "Маша")}
    r = await client.post("/api/add", json={"text": "Ты обещал вкрутить лампочку"}, headers=h)
    item = (await r.json())["items"][0]
    assert item["assignee"] == 2 and item["kind"] == "task"
    state = await (await client.get("/api/state", headers=h)).json()
    assert state["partner"]["name"] == "Дима" and len(state["items"]) == 1
    r = await client.post(f"/api/items/{item['id']}/who", json={"who": "both"}, headers=h)
    assert r.status == 200 and db.item(item["id"], couple)["assignee_id"] is None
    assert (await client.post(f"/api/items/{item['id']}/done", headers=h)).status == 200
    assert not (await (await client.get("/api/state", headers=h)).json())["items"]


async def test_other_couple_cannot_delete(client, couple):
    rows = db.add_item(couple, 1, "task", "Секрет")
    h = {"X-Init-Data": init_data(7, "Чужой")}
    assert (await client.delete(f"/api/items/{rows}", headers=h)).status == 404
    assert db.item(rows, couple)


async def test_new_user_gets_own_couple(client):
    h = {"X-Init-Data": init_data(5, "Аня")}
    state = await (await client.get("/api/state", headers=h)).json()
    assert state["me"]["name"] == "Аня" and state["partner"] is None and db.user(5)["couple_id"]


async def test_names_with_plus_and_percent_still_sign_in(client):
    """The header is checked exactly as Telegram sent it: no second URL-decoding."""
    h = {"X-Init-Data": init_data(8, "A+B 100%")}
    state = await (await client.get("/api/state", headers=h)).json()
    assert state["me"]["name"] == "A+B 100%"


async def test_stranger_sees_only_their_own_empty_couple(client, couple):
    db.add_item(couple, 1, "task", "Секрет пары")
    db.save_user(3, "Посторонний")
    assert db.join(3, "guessed-code") == "unknown"
    state = await (await client.get("/api/state", headers={"X-Init-Data": init_data(3, "Посторонний")})).json()
    assert state["items"] == [] and state["partner"] is None


async def test_add_refuses_small_talk_and_fragments(client, couple):
    h = {"X-Init-Data": init_data(1, "Маша")}
    for text, word in (("Привет", "не похоже"), ("Молоко", "Уточните")):
        r = await client.post("/api/add", json={"text": text}, headers=h)
        assert r.status == 400 and word in (await r.json())["error"]
    assert not db.items(couple)


async def test_api_actions_count_as_activity(client, couple):
    h = {"X-Init-Data": init_data(2, "Дима")}
    await client.post("/api/add", json={"text": "купить хлеб"}, headers=h)
    with db.closing(db._connect()) as c:
        assert [r[0] for r in c.execute("SELECT user_id FROM activity")] == [2]


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text))


async def test_partner_hears_about_tasks_added_or_moved_in_the_mini_app(aiohttp_client, couple):
    bot = FakeBot()
    client = await aiohttp_client(web.build_app(bot))
    h = {"X-Init-Data": init_data(1, "Маша")}
    item = (await (await client.post("/api/add", json={"text": "Ты обещал вкрутить лампочку"}, headers=h)).json())["items"][0]
    assert bot.sent[-1][0] == 2 and "записал(а) на тебя" in bot.sent[-1][1] and "лампочку" in bot.sent[-1][1]
    bot.sent.clear()
    await client.post(f"/api/items/{item['id']}/who", json={"who": "me"}, headers=h)
    assert bot.sent == []  # Маша took it herself: nothing to tell Дима
    await client.post(f"/api/items/{item['id']}/who", json={"who": "both"}, headers=h)
    assert bot.sent[-1][0] == 2 and "на вас обоих" in bot.sent[-1][1]
    await client.post(f"/api/items/{item['id']}/who", json={"who": "partner"}, headers=h)
    assert bot.sent[-1][0] == 2 and "на тебя" in bot.sent[-1][1]


async def test_undo_and_edit_through_the_api(client, couple):
    h = {"X-Init-Data": init_data(1, "Маша")}
    item = (await (await client.post("/api/add", json={"text": "Ты обещал вкрутить лампочку"}, headers=h)).json())["items"][0]
    await client.post(f"/api/items/{item['id']}/done", headers=h)
    assert (await client.post(f"/api/items/{item['id']}/undo", headers=h)).status == 200
    assert (await client.post(f"/api/items/{item['id']}/undo", headers=h)).status == 404  # already back
    r = await client.patch(f"/api/items/{item['id']}", json={"text": "Вкрутить лампочку в ванной", "day": "2030-01-05"}, headers=h)
    body = await r.json()
    assert r.status == 200 and body["item"]["text"] == "Вкрутить лампочку в ванной" and body["item"]["day"] == "2030-01-05"
    r = await client.patch(f"/api/items/{item['id']}", json={"day": "завтра"}, headers=h)
    assert r.status == 400 and (await r.json())["error"] == "Не понял дату"
    assert (await client.patch(f"/api/items/{item['id']}", data="[]", headers=h)).status == 400
    other = {"X-Init-Data": init_data(7, "Чужой")}
    assert (await client.patch(f"/api/items/{item['id']}", json={"text": "x"}, headers=other)).status == 404
    assert (await client.post(f"/api/items/{item['id']}/undo", headers=other)).status == 404


async def test_state_tells_the_bots_today(client, couple, monkeypatch):
    import datetime as dt
    monkeypatch.setattr(db, "local_now", lambda: dt.datetime(2026, 10, 23, 0, 30))
    state = await (await client.get("/api/state", headers={"X-Init-Data": init_data(1, "Маша")})).json()
    assert state["today"] == "2026-10-23"
