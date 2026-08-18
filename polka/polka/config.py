"""Конфигурация Полки: читается из переменных окружения или файла .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # polka/
REPO = ROOT.parent                                      # корень репозитория
DATA_DIR = Path(os.environ.get("POLKA_DATA_DIR") or ROOT / "data")


def load_env(*candidates: Path) -> None:
    """Простой парсер .env — без внешних зависимостей.

    Значения из окружения имеют приоритет: .env только дополняет их.
    """
    for path in candidates:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


@dataclass(frozen=True)
class Config:
    bot_token: str
    chat_id: str
    anthropic_key: str
    model: str
    openai_key: str            # расшифровка голосовых через Whisper
    groq_key: str              # то же самое, но бесплатно
    capture_token: str         # секрет для iOS-команды (Back Tap)
    public_url: str            # https-адрес мини-приложения
    host: str
    port: int
    telegram_api: str          # подменяется в тестах на локальный макет
    voice_api: str             # то же для распознавания речи
    voice_reply: bool          # отвечать ли голосом на надиктованное
    relay_url: str             # доска, через которую приходит двойное касание
    relay_topic: str           # имя доски, оно же секрет
    tts_api: str               # подменяется в тестах
    quiet_from: int            # час начала тишины (не будим ночью)
    quiet_to: int
    timezone_offset: int       # смещение от UTC в часах, для тихих часов

    @property
    def voice(self) -> tuple[str, str, str] | None:
        """Чем расшифровывать голос: (адрес, ключ, модель). None - нечем."""
        if self.groq_key:
            return (self.voice_api or
                    "https://api.groq.com/openai/v1/audio/transcriptions",
                    self.groq_key, "whisper-large-v3-turbo")
        if self.openai_key:
            return (self.voice_api or
                    "https://api.openai.com/v1/audio/transcriptions",
                    self.openai_key, "whisper-1")
        return None

    @property
    def speech(self) -> tuple[str, str] | None:
        """Чем озвучивать вопрос: (адрес, ключ). Умеет только OpenAI:
        у бесплатных сервисов нет русского голоса."""
        if not (self.voice_reply and self.openai_key):
            return None
        return (self.tts_api or "https://api.openai.com/v1/audio/speech",
                self.openai_key)

    @property
    def has_brain(self) -> bool:
        return bool(self.anthropic_key)

    @property
    def has_telegram(self) -> bool:
        return bool(self.bot_token and self.chat_id)


def _int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip() or default)
    except ValueError:
        return default


def load_config() -> Config:
    load_env(ROOT / ".env", REPO / ".env")
    return Config(
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
        chat_id=os.environ.get("POLKA_CHAT_ID", os.environ.get("TELEGRAM_CHAT_ID", "")).strip(),
        anthropic_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        model=os.environ.get("POLKA_MODEL", "claude-opus-5").strip(),
        openai_key=os.environ.get("OPENAI_API_KEY", "").strip(),
        groq_key=os.environ.get("GROQ_API_KEY", "").strip(),
        capture_token=os.environ.get("POLKA_CAPTURE_TOKEN", "").strip(),
        public_url=os.environ.get("POLKA_PUBLIC_URL", "").strip().rstrip("/"),
        host=os.environ.get("POLKA_HOST", "0.0.0.0").strip(),
        telegram_api=os.environ.get("POLKA_TELEGRAM_API",
                                    "https://api.telegram.org").strip().rstrip("/"),
        voice_api=os.environ.get("POLKA_VOICE_API", "").strip(),
        voice_reply=os.environ.get("POLKA_VOICE_REPLY", "1").strip() not in ("0", "нет", ""),
        relay_url=os.environ.get("POLKA_RELAY_URL", "https://ntfy.sh").strip().rstrip("/"),
        relay_topic=os.environ.get("POLKA_RELAY_TOPIC", "").strip(),
        tts_api=os.environ.get("POLKA_TTS_API", "").strip(),
        port=_int("POLKA_PORT", 8443),
        quiet_from=_int("POLKA_QUIET_FROM", 23),
        quiet_to=_int("POLKA_QUIET_TO", 8),
        timezone_offset=_int("POLKA_TZ_OFFSET", 3),
    )
