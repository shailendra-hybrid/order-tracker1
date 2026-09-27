"""One-time interactive login for the Telegram account that will create groups.

    cd /opt/order-tracker && venv/bin/python tg_login.py

Run it as the same OS user the service runs as, so the session file is readable by the service.
"""
import os
from pathlib import Path

from telethon.sync import TelegramClient

base = Path(__file__).resolve().parent
env = base / ".env"
if env.exists():
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

session = os.environ.get("TG_SESSION") or str(base / "data" / "tg")
Path(session).parent.mkdir(parents=True, exist_ok=True)

with TelegramClient(session, int(os.environ["TG_API_ID"]), os.environ["TG_API_HASH"]) as client:
    me = client.get_me()
    print(f"Logged in as {me.first_name} (@{me.username}). Session saved to {session}.session")
