from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import os

import requests


def main() -> int:
    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("TELEGRAM_BOT_TOKEN 환경변수를 먼저 설정하세요.", file=sys.stderr)
        return 2
    response = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=30)
    response.raise_for_status()
    payload = response.json()
    if not payload.get("ok"):
        print(payload, file=sys.stderr)
        return 1
    chats: dict[str, str] = {}
    for update in payload.get("result", []):
        message = update.get("message") or update.get("channel_post") or update.get("my_chat_member") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            label = chat.get("title") or chat.get("username") or chat.get("first_name") or "unknown"
            chats[str(chat["id"])] = str(label)
    if not chats:
        print("채팅 기록이 없습니다. 봇에게 /start를 보낸 뒤 다시 실행하세요.")
        return 1
    for chat_id, label in chats.items():
        print(f"TELEGRAM_CHAT_ID={chat_id}  # {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
