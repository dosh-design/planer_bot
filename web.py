"""Telegram mini-app: the planner page (webapp/index.html) and the JSON API it calls.

Every API call carries Telegram's signed launch data in the X-Init-Data header; the user id comes from there,
never from the request body. Adding goes through planner.add_text, the same path as the chat.
"""
import logging
from pathlib import Path

from aiohttp import web

import bot as chat
import db
import parser
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
        # The header carries Telegram.WebApp.initData as is (a query string); verify() decodes it once.
        user = webapp_auth.verify(request.headers.get("X-Init-Data", ""), config.bot_token)
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
        "author": row["author_id"], "when": planner.when_text(row, today), "day": row["day"], "time": row["time"],
        "end": row["end_time"],
        "hot": planner.is_hot(row, today),
        "chore_done": planner.chore_done(row, today),
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
        invite = await chat.invite_link(request.app[BOT], uid) or ""
    return web.json_response({
        "brand": config.brand,
        "today": today.isoformat(),  # the bot's calendar day (TZ from config), not the phone's
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
    intent = parser.intent(text, db.local_now().date())
    if intent == "chat":
        return _err("Это не похоже на дело. Напишите, что сделать, купить или когда кто занят.")
    if intent == "unclear":
        return _err("Маловато слов. Уточните: «купить молоко» или «вкрутить лампочку до субботы».")
    rows, notes = await planner.add_text_async(request["uid"], text)
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


async def undo(request: web.Request) -> web.Response:
    if not planner.undo_done(request["uid"], _id(request)):
        return _err("Нечего возвращать", 404)
    return web.json_response({"ok": True})


async def change(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return _err("Пустой запрос")
    ok, error = planner.edit(request["uid"], _id(request), {k: body[k] for k in ("text", "day", "time", "end") if k in body})
    if not ok:
        return _err(error, 404 if error == "Запись не найдена" else 400)
    row = db.item(_id(request), db.user(request["uid"])["couple_id"])
    return web.json_response({"item": _item(row, db.local_now().date())})


async def remove(request: web.Request) -> web.Response:
    if not planner.remove(request["uid"], _id(request)):
        return _err("Запись не найдена", 404)
    return web.json_response({"ok": True})


async def who(request: web.Request) -> web.Response:
    try:
        value = (await request.json()).get("who")
    except Exception:
        value = None
    if value not in ("me", "partner", "both") or not planner.reassign(request["uid"], _id(request), value):
        return _err("Не получилось переназначить")
    if request.app.get(BOT):
        row = db.item(_id(request), db.user(request["uid"])["couple_id"])
        await chat.send(request.app[BOT], planner.assigned_notes(request["uid"], row))
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
    app.router.add_post("/api/items/{id}/undo", undo)
    app.router.add_patch("/api/items/{id}", change)
    app.router.add_delete("/api/items/{id}", remove)
    return app
