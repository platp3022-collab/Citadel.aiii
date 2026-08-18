"""Тонкий клиент Telegram Bot API поверх aiohttp."""
from __future__ import annotations

import asyncio
import html
import logging
from typing import Any

import aiohttp

log = logging.getLogger("polka.tg")

API = "https://api.telegram.org"


def esc(text: str) -> str:
    """Экранирование под parse_mode=HTML."""
    return html.escape(str(text or ""), quote=False)


class Telegram:
    def __init__(self, session: aiohttp.ClientSession, token: str, chat_id: str,
                 dry: bool = False, api_base: str = API):
        self.session = session
        self.token = token
        self.chat_id = str(chat_id)
        self.dry = dry or not token
        self.api = f"{api_base}/bot{token}"
        self.file_api = f"{api_base}/file/bot{token}"
        self.offset = 0

    async def call(self, method: str, payload: dict[str, Any] | None = None,
                   timeout: int = 25) -> dict[str, Any] | None:
        if self.dry:
            log.info("[dry] %s %s", method, payload)
            return None
        for attempt in range(3):
            try:
                async with self.session.post(
                    f"{self.api}/{method}", json=payload or {},
                    timeout=aiohttp.ClientTimeout(total=timeout),
                ) as r:
                    data = await r.json(content_type=None)
                    if r.status == 429:
                        wait = int((data.get("parameters") or {}).get("retry_after", 3))
                        await asyncio.sleep(wait)
                        continue
                    if not data.get("ok"):
                        log.warning("TG %s: %s", method, str(data)[:200])
                        return None
                    return data.get("result")
            except asyncio.TimeoutError:
                if method == "getUpdates":
                    return None                       # long polling: норма
                log.warning("TG %s: таймаут", method)
            except Exception as exc:  # noqa: BLE001
                log.warning("TG %s: %s", method, exc)
            await asyncio.sleep(1.5 * (attempt + 1))
        return None

    async def send(self, text: str, buttons: list[list[dict[str, Any]]] | None = None,
                   silent: bool = False, chat_id: str | None = None) -> int | None:
        payload: dict[str, Any] = {
            "chat_id": chat_id or self.chat_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
            "disable_notification": silent,
        }
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        result = await self.call("sendMessage", payload)
        return (result or {}).get("message_id")

    async def edit(self, message_id: int, text: str,
                   buttons: list[list[dict[str, Any]]] | None = None,
                   chat_id: str | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id or self.chat_id,
            "message_id": message_id,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if buttons is not None:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        await self.call("editMessageText", payload)

    async def answer_callback(self, callback_id: str, text: str = "",
                              alert: bool = False) -> None:
        await self.call("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": text[:190],
            "show_alert": alert,
        })

    async def send_document(self, filename: str, content: str, caption: str = "",
                            chat_id: str | None = None) -> None:
        if self.dry:
            log.info("[dry] документ %s (%d символов)", filename, len(content))
            return
        form = aiohttp.FormData()
        form.add_field("chat_id", chat_id or self.chat_id)
        if caption:
            form.add_field("caption", caption[:1000])
            form.add_field("parse_mode", "HTML")
        form.add_field("document", content.encode("utf-8"),
                       filename=filename, content_type="text/markdown")
        try:
            async with self.session.post(f"{self.api}/sendDocument", data=form,
                                         timeout=aiohttp.ClientTimeout(total=60)) as r:
                if r.status != 200:
                    log.warning("sendDocument: %s", (await r.text())[:200])
        except Exception as exc:  # noqa: BLE001
            log.warning("sendDocument: %s", exc)

    async def download_file(self, file_id: str) -> bytes | None:
        info = await self.call("getFile", {"file_id": file_id})
        path = (info or {}).get("file_path")
        if not path:
            return None
        try:
            async with self.session.get(f"{self.file_api}/{path}",
                                        timeout=aiohttp.ClientTimeout(total=90)) as r:
                if r.status != 200:
                    return None
                return await r.read()
        except Exception as exc:  # noqa: BLE001
            log.warning("Скачивание файла: %s", exc)
            return None

    async def poll(self) -> list[dict[str, Any]]:
        if self.dry:
            # Без токена запросов нет, поэтому паузу держим сами, иначе цикл крутится вхолостую.
            await asyncio.sleep(1)
            return []
        result = await self.call("getUpdates", {
            "offset": self.offset,
            "timeout": 25,
            "allowed_updates": ["message", "callback_query"],
        }, timeout=35)
        updates = result or []
        for update in updates:
            self.offset = max(self.offset, update.get("update_id", 0) + 1)
        return updates

    async def set_commands(self) -> None:
        await self.call("setMyCommands", {"commands": [
            {"command": "polka", "description": "открыть полки"},
            {"command": "vazhnoe", "description": "что горит прямо сейчас"},
            {"command": "id", "description": "показать chat id"},
        ]})

    async def set_menu_button(self, url: str) -> None:
        """Кнопка меню в чате открывает мини-приложение."""
        if not url:
            return
        await self.call("setChatMenuButton", {
            "chat_id": self.chat_id,
            "menu_button": {
                "type": "web_app",
                "text": "Полка",
                "web_app": {"url": url},
            },
        })
