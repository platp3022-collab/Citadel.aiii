#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Публичный https-адрес для Mini App без ручной возни.

Telegram открывает Mini App только по https, а бот крутится у тебя на localhost.
Раньше это чинилось руками: поставь cloudflared, запусти туннель, скопируй адрес
в .env. Здесь всё это делает сам бот:

    1. ищет cloudflared в PATH и в папке data/;
    2. если нет — качает официальный бинарник Cloudflare с github.com;
    3. запускает быстрый туннель на локальный порт;
    4. вылавливает из вывода адрес вида https://xxx.trycloudflare.com.

Адрес у быстрого туннеля временный и меняется при каждом запуске — поэтому бот
переустанавливает кнопку панели при каждом старте. Нужен постоянный адрес —
пропиши свой домен в WEBAPP_PUBLIC_URL, тогда туннель не поднимается вообще.

Отключить автотуннель: AUTO_TUNNEL=0 в .env.
"""
from __future__ import annotations

import asyncio
import logging
import os
import platform
import re
import shutil
import stat
import tarfile
import tempfile
from pathlib import Path

log = logging.getLogger("polybot.tunnel")

RELEASE_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download"
TUNNEL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

ASSETS = {
    ("windows", "amd64"): "cloudflared-windows-amd64.exe",
    ("windows", "386"): "cloudflared-windows-386.exe",
    ("windows", "arm64"): "cloudflared-windows-amd64.exe",   # работает через эмуляцию
    ("linux", "amd64"): "cloudflared-linux-amd64",
    ("linux", "arm64"): "cloudflared-linux-arm64",
    ("linux", "arm"): "cloudflared-linux-arm",
    ("darwin", "amd64"): "cloudflared-darwin-amd64.tgz",
    ("darwin", "arm64"): "cloudflared-darwin-arm64.tgz",
}


def platform_key() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {
        "x86_64": "amd64", "amd64": "amd64", "x64": "amd64",
        "aarch64": "arm64", "arm64": "arm64",
        "armv7l": "arm", "armv6l": "arm",
        "i386": "386", "i686": "386", "x86": "386",
    }.get(machine, "amd64")
    return system, arch


def asset_name() -> str | None:
    return ASSETS.get(platform_key())


def find_cloudflared(data_dir: Path) -> Path | None:
    """Уже установленный cloudflared: в системе или скачанный ранее."""
    found = shutil.which("cloudflared")
    if found:
        return Path(found)
    local = data_dir / ("cloudflared.exe" if platform.system() == "Windows" else "cloudflared")
    return local if local.exists() else None


async def download_cloudflared(data_dir: Path, session_factory=None) -> Path | None:
    """Скачать бинарник Cloudflare. None — не вышло (нет сети, нет сборки под систему)."""
    name = asset_name()
    if not name:
        log.warning("нет сборки cloudflared под %s/%s", *platform_key())
        return None

    import aiohttp

    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / ("cloudflared.exe" if platform.system() == "Windows" else "cloudflared")
    url = f"{RELEASE_URL}/{name}"
    log.info("качаю cloudflared: %s", url)

    factory = session_factory or (lambda: aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=180)))
    try:
        async with factory() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    log.warning("не скачать cloudflared: HTTP %s", resp.status)
                    return None
                payload = await resp.read()
    except Exception as exc:
        log.warning("не скачать cloudflared: %s", exc)
        return None

    if name.endswith(".tgz"):
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / name
            archive.write_bytes(payload)
            try:
                with tarfile.open(archive) as tar:
                    member = next((m for m in tar.getmembers()
                                   if m.name.endswith("cloudflared")), None)
                    if not member:
                        return None
                    extracted = tar.extractfile(member)
                    if not extracted:
                        return None
                    target.write_bytes(extracted.read())
            except (tarfile.TarError, OSError) as exc:
                log.warning("архив cloudflared не распаковался: %s", exc)
                return None
    else:
        target.write_bytes(payload)

    if platform.system() != "Windows":
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
    log.info("cloudflared готов: %s", target)
    return target


class Tunnel:
    """Быстрый туннель Cloudflare: публичный https поверх локального порта."""

    def __init__(self, port: int, data_dir: Path) -> None:
        self.port = port
        self.data_dir = data_dir
        self.url: str = ""
        self.proc: asyncio.subprocess.Process | None = None
        self._drain: asyncio.Task | None = None

    async def start(self, timeout: float = 60.0, session_factory=None) -> str:
        """Поднять туннель и вернуть адрес. Пустая строка — не получилось."""
        binary = find_cloudflared(self.data_dir)
        if not binary:
            binary = await download_cloudflared(self.data_dir, session_factory)
        if not binary:
            return ""

        try:
            self.proc = await asyncio.create_subprocess_exec(
                str(binary), "tunnel", "--no-autoupdate",
                "--url", f"http://127.0.0.1:{self.port}",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:
            # сюда попадает и NotImplementedError, если у asyncio нет поддержки
            # подпроцессов в текущем цикле событий — бот от этого падать не должен
            log.warning("cloudflared не запустился: %s", exc)
            return ""

        assert self.proc.stdout is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                line = await asyncio.wait_for(self.proc.stdout.readline(),
                                              timeout=max(1.0, deadline - loop.time()))
            except asyncio.TimeoutError:
                break
            if not line:
                break
            match = TUNNEL_RE.search(line.decode("utf-8", "replace"))
            if match:
                self.url = match.group(0)
                # дальше вывод только копится в трубе — читаем и выбрасываем
                self._drain = asyncio.create_task(self._drain_output())
                log.info("туннель поднят: %s", self.url)
                return self.url

        log.warning("cloudflared не сообщил адрес за %.0f с", timeout)
        await self.stop()
        return ""

    async def _drain_output(self) -> None:
        assert self.proc is not None and self.proc.stdout is not None
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    return
        except asyncio.CancelledError:
            return

    async def stop(self) -> None:
        if self._drain:
            self._drain.cancel()
            self._drain = None
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.terminate()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except (asyncio.TimeoutError, ProcessLookupError, OSError):
                try:
                    self.proc.kill()
                except (ProcessLookupError, OSError):
                    pass
        self.proc = None
        self.url = ""


def auto_tunnel_enabled() -> bool:
    return os.environ.get("AUTO_TUNNEL", "1").strip().lower() not in ("0", "no", "false", "off")
