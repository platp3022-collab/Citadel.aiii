"""Разбор мысли моделью Claude: полка, важность, напоминание, промт для Claude Code."""
from __future__ import annotations

import json
import logging
from typing import Any

try:
    from anthropic import AsyncAnthropic
except ImportError:  # пакет ставится из requirements.txt
    AsyncAnthropic = None  # type: ignore[assignment]

log = logging.getLogger("polka.brain")

KINDS = ("idea", "task", "reminder", "note", "question")

SORT_SYSTEM = """Ты разбираешь поток мыслей одного человека и раскладываешь их по полкам.

Человек диктует на ходу, часто одной фразой, без знаков препинания, с опечатками.
Твоя работа: понять, что он на самом деле имел в виду, и вернуть аккуратную карточку.

Правила:
- Пиши на языке оригинала мысли. Если мысль на русском, вся карточка на русском.
- title: 2-6 слов, как корешок книги на полке. Без точки в конце. Не пересказывай целиком.
- summary: одно предложение по делу. Если мысль и так короткая и ясная, повтори её чище.
  Не выдумывай деталей, которых человек не говорил.
- shelf: выбирай ключ из списка существующих полок. Новую полку заводи, только если
  мысль явно не ложится ни на одну и такие мысли будут повторяться.
- importance: 1 мимолётное, 2 обычное, 3 стоит вернуться, 4 важное, 5 нельзя потерять.
  Не завышай. Пятёрка это дедлайн, здоровье, деньги, обещание другому человеку.
- needs_reminder: true только если без напоминания это реально потеряется или протухнет.
  Идея "когда-нибудь сделать" напоминания не требует.
- reminder.mode: "once" разовое, "every" повторяющееся.
  reminder.interval_min: для "every" период в минутах, для "once" 0.
  reminder.delay_min: через сколько минут первый раз напомнить.
- ask_user: true, если ты не уверен в важности или в сроке и лучше переспросить одним касанием.
- claude_worthy: true, если это идея, которую можно построить руками или кодом,
  и из неё выйдет осмысленное техническое задание.
- tags: 0-4 коротких слова в нижнем регистре.

Никаких длинных тире в тексте. Только обычный дефис."""

PROMPT_SYSTEM = """Ты превращаешь сырую идею человека в подробное техническое задание
для Claude Code, который будет писать этот проект с нуля.

Пиши на языке оригинальной идеи. Формат ответа - готовый промт, который можно
скопировать и вставить в Claude Code целиком. Структура:

1. Что строим: один абзац, суть и для кого.
2. Как это работает для пользователя: 3-6 пунктов реального сценария.
3. Технические решения: стек, хранилище, внешние сервисы, и почему именно так.
4. Объём первой версии: что входит и что осознанно не входит.
5. Открытые вопросы: 2-4 места, где нужно решение человека.

Требования:
- Достраивай недостающие детали сам, но помечай их как предположение.
- Никакой воды и общих слов вроде "современный" и "удобный". Конкретика.
- Никаких длинных тире. Только обычный дефис.
- Не пиши код, пиши задание."""

SORT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "summary": {"type": "string"},
        "shelf": {"type": "string"},
        "new_shelf_title": {"type": "string"},
        "kind": {"type": "string", "enum": list(KINDS)},
        # Строгая схема не принимает minimum/maximum у чисел, диапазоны держит normalize_card.
        "importance": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "needs_reminder": {"type": "boolean"},
        "reminder": {
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["once", "every"]},
                "interval_min": {"type": "integer"},
                "delay_min": {"type": "integer"},
            },
            "required": ["mode", "interval_min", "delay_min"],
            "additionalProperties": False,
        },
        "ask_user": {"type": "boolean"},
        "claude_worthy": {"type": "boolean"},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "title", "summary", "shelf", "new_shelf_title", "kind", "importance",
        "needs_reminder", "reminder", "ask_user", "claude_worthy", "tags",
    ],
    "additionalProperties": False,
}


class Brain:
    """Обёртка над Claude. Без ключа работает в резервном режиме без модели."""

    def __init__(self, api_key: str, model: str):
        self.model = model
        self.client = None
        if api_key and AsyncAnthropic is not None:
            self.client = AsyncAnthropic(api_key=api_key)
        elif api_key:
            log.warning("Пакет anthropic не установлен: pip install anthropic")

    @property
    def enabled(self) -> bool:
        return self.client is not None

    async def sort(self, text: str, shelves: list[dict[str, Any]]) -> dict[str, Any]:
        """Разложить мысль по полке. Всегда возвращает валидную карточку."""
        if not self.client:
            return fallback_card(text)

        shelf_lines = "\n".join(
            f"- {s['key']}: {s['title']} ({s['subtitle']})" if s.get("subtitle")
            else f"- {s['key']}: {s['title']}"
            for s in shelves
        )
        user = f"Существующие полки:\n{shelf_lines}\n\nМысль:\n{text.strip()}"

        try:
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=1200,
                system=SORT_SYSTEM,
                messages=[{"role": "user", "content": user}],
                output_config={
                    "effort": "low",
                    "format": {"type": "json_schema", "schema": SORT_SCHEMA},
                },
            )
        except Exception as exc:  # noqa: BLE001 — модель не должна ронять захват мысли
            log.warning("Claude не ответил на разбор: %s", exc)
            return fallback_card(text)

        raw = next((b.text for b in response.content if b.type == "text"), "")
        try:
            card = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("Claude вернул не JSON: %.200s", raw)
            return fallback_card(text)
        return normalize_card(card, text)

    async def to_claude_prompt(self, title: str, text: str) -> str:
        """Развернуть идею в подробное задание для Claude Code."""
        if not self.client:
            return (
                f"# {title}\n\n{text.strip()}\n\n"
                "Разверни эту идею в техническое задание: что строим, сценарий "
                "пользователя, стек, объём первой версии, открытые вопросы."
            )
        try:
            async with self.client.messages.stream(
                model=self.model,
                max_tokens=8000,
                system=PROMPT_SYSTEM,
                messages=[{"role": "user", "content": f"Идея: {title}\n\n{text.strip()}"}],
                output_config={"effort": "high"},
            ) as stream:
                response = await stream.get_final_message()
        except Exception as exc:  # noqa: BLE001
            log.warning("Claude не собрал промт: %s", exc)
            return f"# {title}\n\n{text.strip()}"
        return "".join(b.text for b in response.content if b.type == "text").strip()


def fallback_card(text: str) -> dict[str, Any]:
    """Карточка без модели: мысль всё равно должна лечь на полку."""
    clean = " ".join(text.split())
    title = clean[:48] + ("..." if len(clean) > 48 else "")
    return {
        "title": title or "Без названия",
        "summary": clean[:280],
        "shelf": "inbox",
        "new_shelf_title": "",
        "kind": "note",
        "importance": 2,
        "needs_reminder": False,
        "reminder": {"mode": "once", "interval_min": 0, "delay_min": 0},
        "ask_user": True,
        "claude_worthy": False,
        "tags": [],
    }


def normalize_card(card: dict[str, Any], text: str) -> dict[str, Any]:
    """Подчистить ответ модели: значения по умолчанию и границы диапазонов."""
    base = fallback_card(text)
    out = {**base, **{k: v for k, v in card.items() if v is not None}}

    out["title"] = str(out.get("title") or base["title"]).strip()[:80]
    out["summary"] = str(out.get("summary") or "").strip()[:400]
    out["shelf"] = str(out.get("shelf") or "inbox").strip().lower()[:32] or "inbox"
    if out.get("kind") not in KINDS:
        out["kind"] = "note"
    try:
        out["importance"] = min(5, max(1, int(out.get("importance", 2))))
    except (TypeError, ValueError):
        out["importance"] = 2

    reminder = out.get("reminder") or {}
    mode = reminder.get("mode") if reminder.get("mode") in ("once", "every") else "once"
    out["reminder"] = {
        "mode": mode,
        "interval_min": _clamp_int(reminder.get("interval_min"), 0, 0, 60 * 24 * 90),
        "delay_min": _clamp_int(reminder.get("delay_min"), 0, 0, 60 * 24 * 365),
    }
    if mode == "every" and out["reminder"]["interval_min"] < 15:
        out["reminder"]["interval_min"] = 15          # не долбить чаще раза в 15 минут

    out["needs_reminder"] = bool(out.get("needs_reminder"))
    out["ask_user"] = bool(out.get("ask_user"))
    out["claude_worthy"] = bool(out.get("claude_worthy"))
    tags = out.get("tags") or []
    out["tags"] = [str(t).strip().lower()[:24] for t in tags if str(t).strip()][:4]
    return out


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        return min(high, max(low, int(value)))
    except (TypeError, ValueError):
        return default
