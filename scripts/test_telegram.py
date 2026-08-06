from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from datetime import datetime
from zoneinfo import ZoneInfo

from src.telegram_client import TelegramClient


def main() -> int:
    client = TelegramClient.from_env()
    identity = client.validate()
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    client.send_text(
        "✅ GitHub Actions ↔ Telegram 연결 테스트 성공\n"
        f"시각: {now.strftime('%Y-%m-%d %H:%M:%S KST')}\n"
        f"Bot: @{identity['bot']}\n대상: {identity['chat']}"
    )
    print(identity)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
