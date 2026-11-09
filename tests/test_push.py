"""Phone app: push tokens, sending, account deletion inside the app."""
import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

import db
import push
from test_web import client, init_data  # noqa: F401  (client is a fixture)

H1 = {"X-Init-Data": init_data(1, "Маша")}
TOKEN = "f" * 64


def _pem(key) -> str:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def test_off_without_keys(monkeypatch):
    for k in ("FCM_SERVICE_ACCOUNT", "APNS_KEY", "APNS_KEY_ID", "APNS_TEAM_ID"):
        monkeypatch.delenv(k, raising=False)
    assert push.enabled() == {"android": False, "ios": False}
    monkeypatch.setenv("FCM_SERVICE_ACCOUNT", "not json")
    assert push.enabled()["android"] is False


def test_bot_html_becomes_a_plain_line():
    assert push.plain("🔔 <b>Маша</b>, &laquo;вынести&raquo;\n мусор") == "🔔 Маша, «вынести» мусор"
    assert len(push.plain("а" * 1000)) == push.MAX_BODY


def test_apple_token_is_signed_es256(monkeypatch):
    key = ec.generate_private_key(ec.SECP256R1())
    push._cache.clear()
    monkeypatch.setenv("APNS_KEY", _pem(key).replace("\n", "\\n"))  # one line, as pasted into Amvera
    monkeypatch.setenv("APNS_KEY_ID", "KID1234567")
    monkeypatch.setenv("APNS_TEAM_ID", "TEAM123456")
    assert push.enabled()["ios"]
    head, claims, sig = push._apns_jwt(push._apns()).split(".")
    pad = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))  # noqa: E731
    assert json.loads(pad(head)) == {"alg": "ES256", "kid": "KID1234567"}
    assert json.loads(pad(claims))["iss"] == "TEAM123456" and len(pad(sig)) == 64


async def test_google_token_request_is_signed(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    acc = {"private_key": _pem(key), "client_email": "x@y.iam.gserviceaccount.com", "project_id": "p"}
    push._cache.clear()
    sent = {}

    class Resp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def json(self):
            return {"access_token": "tok"}

    class Session:
        def post(self, url, data=None, **kw):
            sent.update(url=url, data=data)
            return Resp()
    assert await push._fcm_token(Session(), acc) == "tok"
    assert sent["data"]["assertion"].count(".") == 2
    assert await push._fcm_token(None, acc) == "tok"  # cached: no second request


async def test_register_and_notify_forgets_dead_tokens(client, couple, monkeypatch):  # noqa: F811
    assert (await client.post("/api/push", json={"token": TOKEN, "platform": "android"}, headers=H1)).status == 200
    for bad in ({"token": "x", "platform": "android"}, {"token": TOKEN, "platform": "web"}, {}):
        assert (await client.post("/api/push", json=bad, headers=H1)).status == 400
    assert [t["token"] for t in db.push_tokens(1)] == [TOKEN]
    monkeypatch.setattr(push, "_fcm_account", lambda: {"project_id": "p"})
    got = []

    async def fake(session, acc, token, body):
        got.append(body)
        return False  # the app was removed from the phone
    monkeypatch.setattr(push, "_send_android", fake)
    await push.notify(1, "🔔 <b>Вынести мусор</b>")
    assert got == ["🔔 Вынести мусор"] and db.push_tokens(1) == []


async def test_bot_messages_also_go_as_push(couple, monkeypatch):
    import bot
    from planner import Out
    from test_bot import FakeBot
    seen = []

    async def fake(uid, text):
        seen.append((uid, text))
    monkeypatch.setattr(push, "notify", fake)
    await bot.send(FakeBot(), [Out(2, "💬 Маша: привет")])
    assert seen == [(2, "💬 Маша: привет")]


async def test_state_tells_the_app_which_side_is_on(client, couple, monkeypatch):  # noqa: F811
    monkeypatch.setattr(push, "enabled", lambda: {"android": True, "ios": False})
    st = await (await client.get("/api/state", headers=H1)).json()
    assert st["push"] == {"android": True, "ios": False}


async def test_account_can_be_deleted_in_the_app(client, couple):  # noqa: F811
    db.push_register(1, TOKEN, "ios")
    assert (await client.post("/api/account/delete", json={}, headers=H1)).status == 400  # needs the confirmation
    assert db.user(1)
    assert (await client.post("/api/account/delete", json={"confirm": True}, headers=H1)).status == 200
    assert db.user(1) is None and db.push_tokens(1) == []
    assert db.partner(2) is None and db.user(2)  # the partner stays with the shared records


async def test_logout_stops_this_phones_pushes(client, couple):  # noqa: F811
    code = db.login_start()
    db.login_confirm(code, 1)
    token = db.login_collect(code)
    db.push_register(1, TOKEN, "android")
    await client.post("/auth/logout", json={"push": TOKEN}, headers={"Authorization": "Bearer " + token})
    assert db.push_tokens(1) == []
