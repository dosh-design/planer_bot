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
