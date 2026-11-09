"""Push notifications for the Android and iOS apps: the same messages the bot sends in Telegram.

Android goes through Firebase Cloud Messaging (HTTP v1), iOS straight to Apple (APNs, HTTP/2).
Each side is on only when its keys are in the environment; without them nothing is sent and the apps
do not ask for permission. Keys never touch the code or the logs:
  FCM_SERVICE_ACCOUNT  the Firebase service account JSON (one line)
  APNS_KEY             the .p8 key text; APNS_KEY_ID, APNS_TEAM_ID; APNS_SANDBOX=1 for test builds
"""
import base64
import html
import json
import logging
import os
import re
import time

import aiohttp

import db

log = logging.getLogger(__name__)

BUNDLE_ID = os.environ.get("APP_BUNDLE_ID", "ru.dogovorilis.app")
TITLE = "Договорились"
MAX_BODY = 240


def _fcm_account() -> dict | None:
    raw = os.environ.get("FCM_SERVICE_ACCOUNT", "").strip()
    if not raw:
        return None
    try:
        acc = json.loads(raw)
        return acc if acc.get("private_key") and acc.get("client_email") and acc.get("project_id") else None
    except ValueError:
        log.warning("FCM_SERVICE_ACCOUNT is not valid JSON")
        return None


def _apns() -> dict | None:
    key, kid, team = (os.environ.get(k, "").strip() for k in ("APNS_KEY", "APNS_KEY_ID", "APNS_TEAM_ID"))
    if not (key and kid and team):
        return None
    return {"key": key.replace("\\n", "\n"), "kid": kid, "team": team,
            "host": "api.sandbox.push.apple.com" if os.environ.get("APNS_SANDBOX") == "1" else "api.push.apple.com"}


def enabled() -> dict:
    """Which apps can get pushes now: the app asks for permission only when its side is on."""
    return {"android": _fcm_account() is not None, "ios": _apns() is not None}


def plain(text: str) -> str:
    """The bot's HTML message as a notification line."""
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = " ".join(text.split())
    return text if len(text) <= MAX_BODY else text[:MAX_BODY - 1] + "…"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _jwt(header: dict, claims: dict, sign) -> str:
    head = _b64(json.dumps(header, separators=(",", ":")).encode()) + "." + _b64(json.dumps(claims, separators=(",", ":")).encode())
    return head + "." + _b64(sign(head.encode()))


_cache: dict = {}


async def _fcm_token(session: aiohttp.ClientSession, acc: dict) -> str:
    """An OAuth token for FCM, kept for 50 minutes."""
    if _cache.get("fcm") and _cache["fcm"][1] > time.time():
        return _cache["fcm"][0]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    key = serialization.load_pem_private_key(acc["private_key"].encode(), password=None)
    now = int(time.time())
    assertion = _jwt({"alg": "RS256", "typ": "JWT"},
                     {"iss": acc["client_email"], "scope": "https://www.googleapis.com/auth/firebase.messaging",
                      "aud": "https://oauth2.googleapis.com/token", "iat": now, "exp": now + 3600},
                     lambda m: key.sign(m, padding.PKCS1v15(), hashes.SHA256()))
    async with session.post("https://oauth2.googleapis.com/token",
                            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion}) as r:
        data = await r.json()
    _cache["fcm"] = (data["access_token"], time.time() + 50 * 60)
    return data["access_token"]


def _apns_jwt(cfg: dict) -> str:
    """Apple's provider token, renewed every 40 minutes (Apple wants 20–60)."""
    if _cache.get("apns") and _cache["apns"][1] > time.time():
        return _cache["apns"][0]
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec, utils
    key = serialization.load_pem_private_key(cfg["key"].encode(), password=None)

    def sign(m: bytes) -> bytes:  # ES256 wants r||s, not DER
        r, s = utils.decode_dss_signature(key.sign(m, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")
    token = _jwt({"alg": "ES256", "kid": cfg["kid"]}, {"iss": cfg["team"], "iat": int(time.time())}, sign)
    _cache["apns"] = (token, time.time() + 40 * 60)
    return token


async def _send_android(session, acc, token: str, body: str) -> bool:
    """False when the token is dead (the app was removed): it is forgotten."""
    url = f"https://fcm.googleapis.com/v1/projects/{acc['project_id']}/messages:send"
    msg = {"message": {"token": token, "notification": {"title": TITLE, "body": body},
                       "android": {"priority": "HIGH", "notification": {"channel_id": "default"}}}}
    async with session.post(url, json=msg, headers={"Authorization": "Bearer " + await _fcm_token(session, acc)}) as r:
        if r.status == 404 or (r.status == 400 and "UNREGISTERED" in await r.text()):
            return False
        if r.status != 200:
            log.info("fcm send: %s", r.status)
    return True


async def _send_ios(cfg, token: str, body: str) -> bool:
    import httpx
    async with httpx.AsyncClient(http2=True, timeout=10) as client:
        r = await client.post(f"https://{cfg['host']}/3/device/{token}",
                              json={"aps": {"alert": {"title": TITLE, "body": body}, "sound": "default"}},
                              headers={"authorization": "bearer " + _apns_jwt(cfg), "apns-topic": BUNDLE_ID,
                                       "apns-push-type": "alert", "apns-priority": "10"})
    if r.status_code in (400, 410) and any(w in r.text for w in ("BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic")):
        return False
    if r.status_code != 200:
        log.info("apns send: %s", r.status_code)
    return True


async def notify(user_id: int, text: str) -> None:
    """Send one bot message as a push to every phone where this person installed the app. Never raises."""
    tokens = db.push_tokens(user_id)
    if not tokens:
        return
    body, acc, cfg = plain(text), _fcm_account(), _apns()
    if not body:
        return
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            for t in tokens:
                if t["platform"] == "android" and acc:
                    alive = await _send_android(session, acc, t["token"], body)
                elif t["platform"] == "ios" and cfg:
                    alive = await _send_ios(cfg, t["token"], body)
                else:
                    continue
                if not alive:
                    db.push_forget(t["token"])
    except Exception as e:  # a push is extra: Telegram already has the message
        log.warning("push to %s failed: %s", user_id, type(e).__name__)
