"""Telegram mini-app: the planner page (webapp/index.html) and the JSON API it calls.

Every API call carries Telegram's signed launch data in the X-Init-Data header; the user id comes from there,
never from the request body. Adding goes through planner.add_text, the same path as the chat.
"""
import base64
import binascii
import logging
from pathlib import Path

from aiohttp import web

import bot as chat
import db
import game
import jokes
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


def _item(row, today, names: dict | None = None) -> dict:
    return {
        "id": row["id"], "kind": row["kind"], "text": row["text"], "status": row["status"],
        "assignee": row["assignee_id"], "assignee_name": planner.name(row["assignee_id"]),
        "author": row["author_id"], "when": planner.when_text(row, today), "day": row["day"], "time": row["time"],
        "end": row["end_time"],
        "hot": planner.is_hot(row, today),
        "chore_done": planner.chore_done(row, today),
        "repeat": row["repeat"], "project_id": row["project_id"],
        "project": (names or {}).get(row["project_id"]) if row["project_id"] else None,
        "weekday": row["weekday"],
        "note": row["note"], "remind": row["remind_before"], "section_id": row["section_id"],
        "done_at": row["done_at"], "series": row["series_id"],
        "warnings": planner.warnings(row),
    }


async def state(request: web.Request) -> web.Response:
    uid = request["uid"]
    u = db.user(uid)
    p = db.partner(uid)
    today = db.local_now().date()
    db.roll_yearly(today)
    projects = db.projects(u["couple_id"])
    names = {pr["id"]: pr["name"] for pr in projects}
    invite = ""
    if not p and request.app.get(BOT):
        invite = await chat.invite_link(request.app[BOT], uid) or ""
    return web.json_response({
        "brand": config.brand,
        "today": today.isoformat(),  # the bot's calendar day (TZ from config), not the phone's
        "me": {"id": uid, "name": u["name"], "avatar": u["avatar"]},
        "partner": {"id": p["id"], "name": p["name"], "avatar": p["avatar"]} if p else None,
        "invite": invite,
        "chat_unread": db.chat_unread(u["couple_id"], uid),
        "items": [_item(r, today, names) for r in db.items(u["couple_id"])],
        # history: «Выполнено» in Дела and «Куплено недавно» in Покупки
        "done": [_item(r, today, names) for r in db.done_items(u["couple_id"], "task", 60)]
                + [_item(r, today, names) for r in db.done_items(u["couple_id"], "buy", 30)],
        "projects": projects,
        "score": game.current(u["couple_id"], today),  # the couple's shared weekly match, same as the bot's /season
    })


async def add(request: web.Request) -> web.Response:
    body = await _body(request)
    if body is None or not isinstance(body.get("text", ""), str):
        return _err("Пустой запрос")
    text = body.get("text", "").strip()
    if not text:
        return _err("Напишите, что не забыть")
    intent = parser.intent(text, db.local_now().date())
    if intent == "chat":
        return _err("Это не похоже на дело. Напишите, что сделать, купить или когда кто занят.")
    if intent == "unclear":
        return _err("Маловато слов. Уточните: «купить молоко» или «вкрутить лампочку до субботы».")
    before = db.last_item_id()
    rows, notes = await planner.add_text_async(request["uid"], text)
    if not rows:
        return _err("Не понял, что записать. Скажите проще: что сделать и когда.")
    if request.app.get(BOT):
        await chat.send(request.app[BOT], notes)
    today = db.local_now().date()
    names = {pr["id"]: pr["name"] for pr in db.projects(rows[0]["couple_id"])}
    # existing: a purchase that was already on the list, so the app says «уже в списке», not «записал»
    out = [_item(r, today, names) | {"existing": r["id"] <= before} for r in rows]
    if note := planner.bad_when_note(text, rows):
        out[0]["warnings"] = [note] + out[0]["warnings"]  # the app shows the first warning as a toast
    return web.json_response({"items": out})


def _id(request: web.Request) -> int:
    try:
        return int(request.match_info["id"])
    except ValueError:
        raise web.HTTPNotFound()


async def done(request: web.Request) -> web.Response:
    d = planner.complete(request["uid"], _id(request))
    if not d.ok:
        return _err("Запись не найдена", 404)
    if request.app.get(BOT):
        await chat.send(request.app[BOT], d.notes)
    # scored and toast come from the server's own count: a second tap or a retried request gets scored=false
    return web.json_response({"ok": True, "scored": d.scored, "toast": d.toast,
                              "score": game.current(db.user(request["uid"])["couple_id"], db.local_now().date())})


async def season(request: web.Request) -> web.Response:
    """«Наш сезон»: the current match, the history and the achievements of the caller's own couple only."""
    uid = request["uid"]
    return web.json_response(game.season(db.user(uid)["couple_id"], db.local_now().date())
                             | {"nudge_mode": planner.nudge_mode(uid), "nudge_modes": jokes.NUDGE_MODE_NAMES})


async def settings(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = None
    mode = body.get("nudge_mode") if isinstance(body, dict) else None
    if mode not in jokes.NUDGE_MODES:
        return _err("Такого режима нет")
    db.set_nudge_mode(request["uid"], mode)
    return web.json_response({"nudge_mode": mode})


AVATAR_PREFIX = "data:image/jpeg;base64,"
AVATAR_MAX_BYTES = 40 * 1024  # the app sends a 256 px JPEG, usually 10–25 KB


def _avatar_ok(value) -> bool:
    """Only a real, small JPEG is stored: the app shrinks the photo, anything else is refused, not trusted."""
    if not isinstance(value, str) or not value.startswith(AVATAR_PREFIX):
        return False
    try:
        raw = base64.b64decode(value[len(AVATAR_PREFIX):], validate=True)
    except (binascii.Error, ValueError):
        return False
    return 0 < len(raw) <= AVATAR_MAX_BYTES and raw[:3] == b"\xff\xd8\xff" and raw[-2:] == b"\xff\xd9"


async def avatar(request: web.Request) -> web.Response:
    """Set (a JPEG data: URL) or remove (null) the caller's own photo. Only the caller's own: the id is from the signature."""
    body = await _body(request)
    if body is None or "image" not in body:
        return _err("Пустой запрос")
    image = body["image"]
    if image is not None and not _avatar_ok(image):
        return _err("Не получилось: нужна фотография JPEG до 40 КБ")
    db.set_avatar(request["uid"], image)
    return web.json_response({"avatar": image})


def _msg(r) -> dict:
    return {"id": r["id"], "author": r["author_id"], "text": r["text"], "at": r["created"]}


async def chat_get(request: web.Request) -> web.Response:
    """The couple's chat; ?after=<id> gives only what is newer (the app polls with it). Reading marks it seen."""
    u = db.user(request["uid"])
    try:
        after = max(0, int(request.query.get("after", 0)))
    except ValueError:
        after = 0
    rows = db.messages(u["couple_id"], after)
    if rows:
        db.chat_seen(request["uid"], rows[-1]["id"])
    return web.json_response({"messages": [_msg(r) for r in rows]})


async def chat_post(request: web.Request) -> web.Response:
    body = await _body(request)
    text = body.get("text") if body else None
    if not isinstance(text, str) or not text.strip():
        return _err("Пустое сообщение")
    if len(text) > db.CHAT_MAX:
        return _err(f"Слишком длинно: до {db.CHAT_MAX} знаков")
    uid = request["uid"]
    u = db.user(uid)
    row = db.add_message(u["couple_id"], uid, text.strip())
    db.chat_seen(uid, row["id"])
    p = db.partner(uid)
    if p and request.app.get(BOT):  # the partner hears it in Telegram right away, as a short line
        await chat.send(request.app[BOT], [planner.Out(p["id"], f"💬 {planner.esc(u['name'])}: {planner.esc(row['text'])}")])
    return web.json_response({"message": _msg(row)})


async def undo(request: web.Request) -> web.Response:
    if not planner.undo_done(request["uid"], _id(request)):
        return _err("Нечего возвращать", 404)
    return web.json_response({"ok": True})


EDIT_KEYS = ("text", "day", "time", "end", "note", "remind", "repeat", "section")


async def change(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return _err("Пустой запрос")
    if "project_id" in body:  # into a project or out of it; may come alone or with other changes
        pid = body["project_id"]
        if pid is not None and not isinstance(pid, int) or not planner.move_to_project(request["uid"], _id(request), pid):
            return _err("Не получилось перенести в проект")
        if not any(k in body for k in EDIT_KEYS):
            return web.json_response({"item": _row_json(request)})
    ok, error = planner.edit(request["uid"], _id(request), {k: body[k] for k in EDIT_KEYS if k in body})
    if not ok:
        return _err(error, 404 if error == "Запись не найдена" else 400)
    return web.json_response({"item": _row_json(request)})


async def move(request: web.Request) -> web.Response:
    body = await _body(request) or {}
    if not planner.move(request["uid"], _id(request), body.get("dir")):
        return _err("Дальше двигать некуда")
    return web.json_response({"ok": True})


def _row_json(request: web.Request) -> dict:
    cid = db.user(request["uid"])["couple_id"]
    row = db.item(_id(request), cid)
    pr = db.project(row["project_id"], cid) if row["project_id"] else None
    return _item(row, db.local_now().date(), {pr["id"]: pr["name"]} if pr else None)


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


# ---------- projects ----------

async def _body(request: web.Request) -> dict | None:
    try:
        body = await request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


def _project_json(request: web.Request, pid: int) -> dict | None:
    cid = db.user(request["uid"])["couple_id"]
    pr = next((x for x in db.projects(cid) if x["id"] == pid), None)
    if not pr:
        return None
    today = db.local_now().date()
    return {**pr, "sections": [{"id": r["id"], "name": r["name"]} for r in db.sections(pid, cid)],
            "items": [_item(r, today, {pid: pr["name"]}) for r in db.project_items(pid, cid)]}


async def project_create(request: web.Request) -> web.Response:
    body = await _body(request) or {}
    pid, error = planner.create_project(request["uid"], body.get("name", ""), body.get("about", ""))
    if not pid:
        return _err(error)
    return web.json_response({"project": _project_json(request, pid)})


async def project_get(request: web.Request) -> web.Response:
    data = _project_json(request, _id(request))
    return web.json_response({"project": data}) if data else _err("Проект не найден", 404)


async def project_change(request: web.Request) -> web.Response:
    body = await _body(request)
    if body is None:
        return _err("Пустой запрос")
    ok, error = planner.edit_project(request["uid"], _id(request), {k: body[k] for k in ("name", "about", "start", "end") if k in body})
    if not ok:
        return _err(error, 404 if error == "Проект не найден" else 400)
    return web.json_response({"project": _project_json(request, _id(request))})


async def project_remove(request: web.Request) -> web.Response:
    if not planner.delete_project(request["uid"], _id(request)):
        return _err("Проект не найден", 404)
    return web.json_response({"ok": True})


async def project_add(request: web.Request) -> web.Response:
    body = await _body(request) or {}
    if not isinstance(body.get("text", ""), str):
        return _err("Напишите задачу")
    section = body.get("section")
    if section is not None and (not isinstance(section, int) or isinstance(section, bool)):
        return _err("Этап не найден")
    rows, notes, error = planner.add_to_project(request["uid"], _id(request), body.get("text", ""), body.get("who", "me"),
                                                section)
    if error:
        return _err(error, 404 if error == "Проект не найден" else 400)
    if request.app.get(BOT):
        await chat.send(request.app[BOT], notes)
    return web.json_response({"project": _project_json(request, _id(request))})


async def section_add(request: web.Request) -> web.Response:
    body = await _body(request) or {}
    sid, error = planner.add_section(request["uid"], _id(request), body.get("name", ""))
    if not sid:
        return _err(error, 404 if error == "Проект не найден" else 400)
    return web.json_response({"project": _project_json(request, _id(request))})


def _section_project(request: web.Request) -> int | None:
    sec = db.section(_id(request), db.user(request["uid"])["couple_id"])
    return sec["project_id"] if sec else None


async def section_change(request: web.Request) -> web.Response:
    body = await _body(request) or {}
    pid = _section_project(request)
    ok, error = planner.rename_section(request["uid"], _id(request), body.get("name", ""))
    if not ok:
        return _err(error, 404 if error == "Этап не найден" else 400)
    return web.json_response({"project": _project_json(request, pid)})


async def section_remove(request: web.Request) -> web.Response:
    pid = _section_project(request)
    if not planner.delete_section(request["uid"], _id(request)):
        return _err("Этап не найден", 404)
    return web.json_response({"project": _project_json(request, pid)})


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
    app.router.add_get("/api/season", season)
    app.router.add_post("/api/settings", settings)
    app.router.add_post("/api/avatar", avatar)
    app.router.add_get("/api/chat", chat_get)
    app.router.add_post("/api/chat", chat_post)
    app.router.add_post("/api/projects", project_create)
    app.router.add_get("/api/projects/{id}", project_get)
    app.router.add_patch("/api/projects/{id}", project_change)
    app.router.add_delete("/api/projects/{id}", project_remove)
    app.router.add_post("/api/projects/{id}/items", project_add)
    app.router.add_post("/api/projects/{id}/sections", section_add)
    app.router.add_patch("/api/sections/{id}", section_change)
    app.router.add_delete("/api/sections/{id}", section_remove)
    app.router.add_post("/api/items/{id}/move", move)
    return app
