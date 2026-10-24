"""Telegram mini-app: the planner page (webapp/index.html) and the JSON API it calls.

Every API call carries Telegram's signed launch data in the X-Init-Data header; the user id comes from there,
never from the request body. Adding goes through planner.add_text, the same path as the chat.
"""
import logging
from pathlib import Path
from urllib.parse import unquote

from aiohttp import web

import bot as chat
import db
import planner
import webapp_auth
from config import config

STATIC = Path(__file__).parent / "webapp"
BOT = web.AppKey("bot", object)
log = logging.getLogger(__name__)


def _err(text: str, status: int = 400) -> web.Response:
    return web.json_response({"error": text}, status=status)


@web.middleware
async def auth(request: web.Request, handler):
    if request.path.startswith("/api/"):
        user = webapp_auth.verify(unquote(request.headers.get("X-Init-Data", "")), config.bot_token)
        if not user:
            return _err("Откройте планер из Telegram-бота.", 401)
        uid = int(user["id"])
        if not db.user(uid):
            db.save_user(uid, (user.get("first_name") or user.get("username") or "Я").strip())
        if not db.user(uid)["couple_id"]:
            db.couple_code(uid)
        request["uid"] = uid
    return await handler(request)


def _item(row, today) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "text": row["text"], "status": row["status"],
        "assignee": row["assignee_id"], "assignee_name": planner.name(row["assignee_id"]),
        "author": row["author_id"], "when": planner.when_text(row, today), "day": row["day"],
        "hot": planner.is_hot(row, today),
        "weekday": row["weekday"],
        "warnings": planner.warnings(row),
    }


async def state(request: web.Request) -> web.Response:
    uid = request["uid"]
    u = db.user(uid)
    p = db.partner(uid)
    today = db.local_now().date()
    invite = ""
    if not p and request.app.get(BOT):
        invite = await chat.invite_link(request.app[BOT], uid)
    return web.json_response({
        "brand": config.brand,
        "me": {"id": uid, "name": u["name"]},
        "partner": {"id": p["id"], "name": p["name"]} if p else None,
        "invite": invite,
        "items": [_item(r, today) for r in db.items(u["couple_id"])],
    })


async def add(request: web.Request) -> web.Response:
    try:
        text = str((await request.json()).get("text", "")).strip()
    except Exception:
        return _err("Пустой запрос")
    if not text:
        return _err("Напишите, что не забыть")
    rows, notes = planner.add_text(request["uid"], text)
    if not rows:
        return _err("Не понял, что записать. Скажите проще: что сделать и когда.")
    if request.app.get(BOT):
        await chat.send(request.app[BOT], notes)
    today = db.local_now().date()
    return web.json_response({"items": [_item(r, today) for r in rows]})


def _id(request: web.Request) -> int:
    try:
        return int(request.match_info["id"])
    except ValueError:
        raise web.HTTPNotFound()


async def done(request: web.Request) -> web.Response:
    ok, notes = planner.done(request["uid"], _id(request))
    if not ok:
        return _err("Запись не найдена", 404)
    if request.app.get(BOT):
        await chat.send(request.app[BOT], notes)
    return web.json_response({"ok": True})


async def remove(request: web.Request) -> web.Response:
    u = db.user(request["uid"])
    if not db.delete_item(_id(request), u["couple_id"]):
        return _err("Запись не найдена", 404)
    return web.json_response({"ok": True})


async def who(request: web.Request) -> web.Response:
    try:
        value = (await request.json()).get("who")
    except Exception:
        value = None
    if value not in ("me", "partner", "both") or not planner.reassign(request["uid"], _id(request), value):
        return _err("Не получилось переназначить")
    return web.json_response({"ok": True})


async def page(_):
    return web.FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def health(_):
    return web.Response(text="ok")


def build_app(bot=None) -> web.Application:
    app = web.Application(middlewares=[auth], client_max_size=64 * 1024)
    if bot is not None:
        app[BOT] = bot
    app.router.add_get("/", page)
    app.router.add_get("/tg", page)
    app.router.add_get("/health", health)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/add", add)
    app.router.add_post("/api/items/{id}/done", done)
    app.router.add_post("/api/items/{id}/who", who)
    app.router.add_delete("/api/items/{id}", remove)
    return app
