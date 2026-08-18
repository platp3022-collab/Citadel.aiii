"""HTTP: API мини-приложения, точка входа для команды быстрого доступа, статика."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

from aiohttp import web

from .config import Config
from .db import Store
from .flow import REMINDER_PRESETS, Flow

log = logging.getLogger("polka.api")

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
INIT_DATA_TTL = 24 * 3600


def check_init_data(init_data: str, bot_token: str) -> dict[str, Any] | None:
    """Проверка подписи Telegram WebApp. Возвращает разобранные поля или None."""
    if not (init_data and bot_token):
        return None
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received = pairs.pop("hash", "")
    if not received:
        return None
    check_string = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > INIT_DATA_TTL:
            return None
    except ValueError:
        return None
    return pairs


@web.middleware
async def errors_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except web.HTTPException:
        raise
    except (KeyError, ValueError) as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка в %s", request.path)
        return web.json_response({"error": "internal", "detail": str(exc)}, status=500)


def build_app(cfg: Config, store: Store, flow: Flow) -> web.Application:
    app = web.Application(middlewares=[errors_middleware])

    def authorize(request: web.Request) -> str:
        """Пускаем либо подписанное мини-приложение, либо ключ команды iOS."""
        init_data = request.headers.get("X-Telegram-Init-Data", "")
        if init_data:
            parsed = check_init_data(init_data, cfg.bot_token)
            if not parsed:
                raise web.HTTPUnauthorized(text="подпись не сходится")
            user = json.loads(parsed.get("user") or "{}")
            if cfg.chat_id and str(user.get("id")) != cfg.chat_id:
                raise web.HTTPForbidden(text="чужой аккаунт")
            return "miniapp"

        token = request.headers.get("X-Polka-Token", "")
        if not token:
            auth = request.headers.get("Authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
        if cfg.capture_token and hmac.compare_digest(token, cfg.capture_token):
            return "shortcut"
        raise web.HTTPUnauthorized(text="нужен ключ")

    async def health(_: web.Request) -> web.Response:
        return web.json_response({
            "ok": True,
            "brain": flow.brain.enabled,
            "telegram": cfg.has_telegram,
        })

    async def state(request: web.Request) -> web.Response:
        authorize(request)
        thoughts = await store.list_thoughts()
        shelves = await store.list_shelves()
        reminders = await store.list_reminders()
        by_thought: dict[str, list[dict[str, Any]]] = {}
        for reminder in reminders:
            by_thought.setdefault(reminder["thought_id"], []).append(reminder)

        counts: dict[str, int] = {}
        for thought in thoughts:
            if thought["status"] != "done":
                counts[thought["shelf"]] = counts.get(thought["shelf"], 0) + 1

        return web.json_response({
            "now": time.time(),
            "shelves": [
                {**shelf, "count": counts.get(shelf["key"], 0)} for shelf in shelves
            ],
            "thoughts": [
                {
                    "id": t["id"],
                    "title": t["title"] or t["raw"][:60],
                    "summary": t["summary"],
                    "raw": t["raw"],
                    "shelf": t["shelf"],
                    "kind": t["kind"],
                    "importance": t["importance"],
                    "status": t["status"],
                    "tags": json.loads(t["tags"] or "[]"),
                    "claudeWorthy": bool(t["claude_worthy"]),
                    "hasPrompt": bool(t["claude_prompt"]),
                    "createdAt": t["created_at"],
                    "reminders": [
                        {
                            "id": r["id"],
                            "mode": r["mode"],
                            "intervalMin": r["interval_min"],
                            "nextFireAt": r["next_fire_at"],
                            "escalation": r["escalation"],
                        }
                        for r in by_thought.get(t["id"], [])
                    ],
                }
                for t in thoughts
            ],
            "presets": [
                {"code": code, "label": preset[0]}
                for code, preset in REMINDER_PRESETS.items()
            ],
        })

    async def capture(request: web.Request) -> web.Response:
        source = authorize(request)
        body = await request.json()
        text = (body.get("text") or "").strip()
        if not text:
            raise ValueError("пустая мысль")
        notify = bool(body.get("notify", source == "shortcut"))
        thought = await flow.capture(
            text, source="shortcut" if source == "shortcut" else "miniapp",
            notify=notify,
        )
        return web.json_response({
            "id": thought["id"],
            "title": thought.get("title", ""),
            "shelf": thought.get("shelf", "inbox"),
            "importance": thought.get("importance", 2),
        })

    async def set_importance(request: web.Request) -> web.Response:
        authorize(request)
        body = await request.json()
        value = min(5, max(1, int(body.get("importance", 2))))
        await store.update_thought(request.match_info["tid"], importance=value)
        return web.json_response({"ok": True, "importance": value})

    async def set_reminder(request: web.Request) -> web.Response:
        authorize(request)
        body = await request.json()
        label = await flow.set_reminder_preset(
            request.match_info["tid"], str(body.get("code", "off"))
        )
        return web.json_response({"ok": True, "label": label})

    async def set_status(request: web.Request) -> web.Response:
        authorize(request)
        body = await request.json()
        status = str(body.get("status", "shelved"))
        if status not in ("shelved", "done", "deleted"):
            raise ValueError("неизвестный статус")
        thought_id = request.match_info["tid"]
        await store.update_thought(thought_id, status=status)
        if status in ("done", "deleted"):
            await store.drop_reminders(thought_id)
        return web.json_response({"ok": True, "status": status})

    async def make_prompt(request: web.Request) -> web.Response:
        authorize(request)
        thought_id = request.match_info["tid"]
        prompt = await flow.build_prompt(thought_id)
        if request.query.get("send") == "1":
            thought = await store.get_thought(thought_id)
            if thought:
                await flow.tg.send_document(
                    f"{thought_id}.md", prompt,
                    caption=f"<b>{thought['title']}</b>\nЗадание для Claude Code",
                )
        return web.json_response({"prompt": prompt})

    async def ack(request: web.Request) -> web.Response:
        authorize(request)
        await store.ack_reminder(request.match_info["rid"])
        return web.json_response({"ok": True})

    async def index(_: web.Request) -> web.StreamResponse:
        page = STATIC_DIR / "index.html"
        if not page.is_file():
            return web.Response(
                text="Мини-приложение не собрано. Запусти: npm --prefix polka/miniapp "
                     "install && npm --prefix polka/miniapp run build",
                content_type="text/plain",
            )
        return web.FileResponse(page)

    app.router.add_get("/health", health)
    app.router.add_get("/api/state", state)
    app.router.add_post("/api/capture", capture)
    app.router.add_post("/api/thoughts/{tid}/importance", set_importance)
    app.router.add_post("/api/thoughts/{tid}/reminder", set_reminder)
    app.router.add_post("/api/thoughts/{tid}/status", set_status)
    app.router.add_post("/api/thoughts/{tid}/prompt", make_prompt)
    app.router.add_post("/api/reminders/{rid}/ack", ack)
    app.router.add_get("/", index)
    if STATIC_DIR.is_dir():
        app.router.add_static("/assets", STATIC_DIR / "assets", follow_symlinks=False)
    return app
