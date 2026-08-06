from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Iterable

import requests


class TelegramError(RuntimeError):
    pass


def split_message(text: str, limit: int = 3900) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = line if not current else current + "\n" + line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


class TelegramClient:
    def __init__(self, token: str, chat_id: str, timeout: int = 30) -> None:
        token = token.strip()
        chat_id = chat_id.strip()
        if not token or not chat_id:
            raise TelegramError("TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 비어 있습니다.")
        self.chat_id = chat_id
        self.timeout = timeout
        self.base_url = f"https://api.telegram.org/bot{token}"
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "github-market-screeners/1.0"})

    @classmethod
    def from_env(cls) -> "TelegramClient":
        return cls(
            token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        )

    def _request(self, method: str, *, data: dict[str, Any] | None = None, files: Any = None, retries: int = 3) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(retries):
            try:
                if files:
                    for value in files.values():
                        handle = value[1] if isinstance(value, tuple) and len(value) > 1 else value
                        if hasattr(handle, "seek"):
                            handle.seek(0)
                response = self.session.post(
                    f"{self.base_url}/{method}",
                    data=data,
                    files=files,
                    timeout=self.timeout,
                )
                try:
                    payload = response.json()
                except ValueError as exc:
                    raise TelegramError(f"Telegram 비정상 응답 HTTP {response.status_code}: {response.text[:300]}") from exc
                if response.ok and payload.get("ok"):
                    return payload
                retry_after = ((payload.get("parameters") or {}).get("retry_after"))
                description = payload.get("description", "알 수 없는 오류")
                if response.status_code == 429 and retry_after and attempt < retries - 1:
                    time.sleep(min(float(retry_after) + 1, 30))
                    continue
                raise TelegramError(f"Telegram {method} 실패 HTTP {response.status_code}: {description}")
            except (requests.RequestException, TelegramError) as exc:
                last_error = exc
                if attempt < retries - 1:
                    time.sleep(2 ** attempt)
        raise TelegramError(str(last_error or "Telegram 요청 실패"))

    def validate(self) -> dict[str, str]:
        me = self._request("getMe", data={})["result"]
        chat = self._request("getChat", data={"chat_id": self.chat_id})["result"]
        return {
            "bot": str(me.get("username") or me.get("first_name") or me.get("id")),
            "chat": str(chat.get("title") or chat.get("username") or chat.get("first_name") or chat.get("id")),
        }

    def send_text(self, text: str, *, silent: bool = False) -> None:
        chunks = split_message(text)
        for index, chunk in enumerate(chunks, start=1):
            prefix = f"({index}/{len(chunks)})\n" if len(chunks) > 1 else ""
            self._request(
                "sendMessage",
                data={
                    "chat_id": self.chat_id,
                    "text": prefix + chunk,
                    "disable_web_page_preview": "true",
                    "disable_notification": "true" if silent else "false",
                },
            )

    def send_document(self, path: Path, caption: str = "") -> None:
        if not path.is_file():
            raise TelegramError(f"첨부 파일 없음: {path}")
        if path.stat().st_size > 49 * 1024 * 1024:
            raise TelegramError(f"Telegram 50MB 제한 초과: {path.name}")
        with path.open("rb") as handle:
            self._request(
                "sendDocument",
                data={"chat_id": self.chat_id, "caption": caption[:1000]},
                files={"document": (path.name, handle)},
            )

    def send_documents(self, paths: Iterable[Path], caption_prefix: str = "") -> list[str]:
        errors: list[str] = []
        for path in paths:
            try:
                self.send_document(path, f"{caption_prefix} {path.name}".strip())
            except Exception as exc:
                errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
        return errors
