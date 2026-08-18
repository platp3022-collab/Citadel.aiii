"""Приём сообщений Telegram: текст, голос, нажатия кнопок."""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from .config import Config
from .db import Store
from .flow import IMPORTANCE_WORD, Flow
from .telegram import Telegram, esc

log = logging.getLogger("polka.bot")

GREETING = (
    "<b>Полка</b>\n\n"
    "Кидай сюда мысль в любом виде. Я пойму, что это, положу на нужную полку "
    "и сам спрошу, когда напомнить.\n\n"
    "Голосом тоже можно. Двойное касание задней крышки айфона настраивается "
    "командой быстрого доступа, тогда диктовать можно не открывая телефон."
)


class Bot:
    def __init__(self, cfg: Config, store: Store, flow: Flow, tg: Telegram,
                 session: aiohttp.ClientSession):
        self.cfg = cfg
        self.store = store
        self.flow = flow
        self.tg = tg
        self.session = session

    async def run(self, stop: asyncio.Event) -> None:
        await self.tg.set_commands()
        if self.cfg.public_url:
            await self.tg.set_menu_button(self.cfg.public_url)
        log.info("Бот слушает Telegram")
        while not stop.is_set():
            try:
                updates = await self.tg.poll()
            except Exception as exc:  # noqa: BLE001
                log.warning("Опрос Telegram: %s", exc)
                await asyncio.sleep(3)
                continue
            for update in updates:
                try:
                    await self.handle(update)
                except Exception:  # noqa: BLE001
                    log.exception("Обработка обновления")

    async def handle(self, update: dict[str, Any]) -> None:
        if "callback_query" in update:
            await self.on_callback(update["callback_query"])
        elif "message" in update:
            await self.on_message(update["message"])

    # ── сообщения ─────────────────────────────────────────────────────────
    async def on_message(self, message: dict[str, Any]) -> None:
        chat_id = str((message.get("chat") or {}).get("id", ""))
        if self.cfg.chat_id and chat_id != self.cfg.chat_id:
            log.info("Чужой чат %s, игнорирую", chat_id)
            return

        text = (message.get("text") or message.get("caption") or "").strip()

        if text.startswith("/"):
            await self.on_command(text, chat_id)
            return

        voice = message.get("voice") or message.get("audio") or message.get("video_note")
        if voice:
            text = await self.transcribe(voice.get("file_id", "")) or ""
            if not text:
                await self.tg.send(
                    "Голос пока не разбираю: не задан ключ распознавания.\n\n"
                    "Бесплатный ключ берётся тут: console.groq.com/keys\n"
                    "Потом впиши в polka/.env строку GROQ_API_KEY=ключ и перезапусти.\n\n"
                    "Либо надиктуй через двойное касание крышки: там речь "
                    "распознаёт сам айфон."
                )
                return

        if not text:
            return
        await self.flow.capture(text, source="telegram")

    async def on_command(self, text: str, chat_id: str) -> None:
        command = text.split()[0].lstrip("/").split("@")[0].lower()
        if command in ("start", "help", "polka"):
            buttons = None
            if self.cfg.public_url:
                buttons = [[{"text": "Открыть полку",
                             "web_app": {"url": self.cfg.public_url}}]]
            await self.tg.send(GREETING, buttons=buttons)
        elif command in ("vazhnoe", "hot"):
            await self.tg.send(await self.flow.burning())
        elif command == "id":
            await self.tg.send(f"chat id: <code>{esc(chat_id)}</code>")
        else:
            # Не команда, а мысль, начинающаяся со слэша.
            await self.flow.capture(text, source="telegram")

    # ── кнопки ────────────────────────────────────────────────────────────
    async def on_callback(self, query: dict[str, Any]) -> None:
        data = query.get("data") or ""
        callback_id = query.get("id") or ""
        message = query.get("message") or {}
        message_id = message.get("message_id")
        parts = data.split(":")
        action = parts[0]

        if action == "imp" and len(parts) == 3:
            thought_id, value = parts[1], int(parts[2])
            await self.store.update_thought(thought_id, importance=value)
            thought = await self.store.get_thought(thought_id)
            if thought and message_id:
                await self.tg.edit(
                    message_id, self.flow.render_card(thought),
                    self.flow.card_buttons(thought_id, value,
                                           bool(thought["claude_worthy"])),
                )
            await self.tg.answer_callback(callback_id,
                                          f"важность {value}, {IMPORTANCE_WORD[value]}")

        elif action == "rem" and len(parts) == 3:
            thought_id, code = parts[1], parts[2]
            label = await self.flow.set_reminder_preset(thought_id, code)
            thought = await self.store.get_thought(thought_id)
            if thought and message_id:
                body = self.flow.render_card(thought)
                body += f"\n\nнапоминание: {esc(label)}"
                await self.tg.edit(
                    message_id, body,
                    self.flow.card_buttons(thought_id, int(thought["importance"]),
                                           bool(thought["claude_worthy"])),
                )
            await self.tg.answer_callback(callback_id, label)

        elif action == "ack" and len(parts) == 2:
            await self.store.ack_reminder(parts[1])
            reminder = await self.store.query(
                "SELECT thought_id FROM reminders WHERE id = ?", (parts[1],))
            if reminder:
                await self.store.update_thought(reminder[0]["thought_id"], status="done")
            if message_id:
                await self.tg.edit(message_id, "Закрыто. Больше не побеспокою.", [])
            await self.tg.answer_callback(callback_id, "закрыто")

        elif action == "snz" and len(parts) == 3:
            await self.store.snooze_reminder(parts[1], int(parts[2]))
            human = "через час" if parts[2] == "60" else "завтра"
            if message_id:
                await self.tg.edit(message_id, f"Вернусь {human}.", [])
            await self.tg.answer_callback(callback_id, human)

        elif action == "stop" and len(parts) == 2:
            await self.store.ack_reminder(parts[1])
            if message_id:
                await self.tg.edit(message_id, "Напоминание снято.", [])
            await self.tg.answer_callback(callback_id, "снято")

        elif action == "prompt" and len(parts) == 2:
            await self.tg.answer_callback(callback_id, "собираю задание")
            await self.send_prompt(parts[1])

        else:
            await self.tg.answer_callback(callback_id)

    async def send_prompt(self, thought_id: str) -> None:
        thought = await self.store.get_thought(thought_id)
        if not thought:
            return
        prompt = await self.flow.build_prompt(thought_id)
        name = "".join(c for c in thought["title"].lower().replace(" ", "-")
                       if c.isalnum() or c in "-_")[:40] or "ideya"
        await self.tg.send_document(
            f"{name}.md", prompt,
            caption=f"<b>{esc(thought['title'])}</b>\nЗадание для Claude Code",
        )

    # ── голос ─────────────────────────────────────────────────────────────
    async def transcribe(self, file_id: str) -> str | None:
        """Расшифровка голосового. Claude аудио на вход не принимает,
        поэтому речь переводит в текст Whisper: у Groq он бесплатный."""
        voice = self.cfg.voice
        if not (file_id and voice):
            return None
        url, key, model = voice

        audio = await self.tg.download_file(file_id)
        if not audio:
            return None

        form = aiohttp.FormData()
        form.add_field("file", audio, filename="voice.ogg", content_type="audio/ogg")
        form.add_field("model", model)
        form.add_field("language", "ru")
        try:
            async with self.session.post(
                url, data=form,
                headers={"Authorization": f"Bearer {key}"},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as r:
                if r.status != 200:
                    log.warning("Расшифровка %s: %s", r.status, (await r.text())[:200])
                    return None
                data = await r.json(content_type=None)
        except Exception as exc:  # noqa: BLE001
            log.warning("Расшифровка: %s", exc)
            return None
        return (data.get("text") or "").strip() or None
