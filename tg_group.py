"""
Create a Telegram supergroup for an order and return its invite link.

Why a user account and not a bot: the Telegram Bot API cannot create groups.
Group creation needs an MTProto *user* session (Telethon). Use a dedicated
company Telegram account, log it in once with `python tg_login.py`, and keep the
session file (data/tg.session) private.

Env: TG_API_ID, TG_API_HASH (from https://my.telegram.org -> API development tools),
     TG_SESSION (optional, default data/tg)
"""
import asyncio
import os
import threading

from telethon import TelegramClient, functions

_lock = threading.Lock()  # one Telegram session file -> one creation at a time


def _session_path():
    return os.environ.get("TG_SESSION") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "tg")


def create_group(title, about="", add_users=(), welcome=None):
    """Blocking helper (runs its own event loop). Returns {'chat_id', 'link', 'add_failed'}."""
    with _lock:
        return asyncio.run(_create(title, about, list(add_users), welcome))


async def _create(title, about, add_users, welcome):
    api_id = int(os.environ["TG_API_ID"])
    api_hash = os.environ["TG_API_HASH"]
    client = TelegramClient(_session_path(), api_id, api_hash)
    await client.connect()  # deliberately not start(): never prompt for a login inside the server
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session not logged in - run tg_login.py on the server")

        res = await client(functions.channels.CreateChannelRequest(title=title, about=about, megagroup=True))
        channel = res.chats[0]

        add_failed = []
        for u in add_users:
            try:
                ent = await client.get_input_entity(u)
                await client(functions.channels.InviteToChannelRequest(channel, [ent]))
            except Exception as e:  # privacy settings, unknown username, not a contact ...
                add_failed.append(f"{u} ({type(e).__name__})")

        invite = await client(functions.messages.ExportChatInviteRequest(peer=channel))
        if welcome:
            try:
                await client.send_message(channel, welcome)
            except Exception:
                pass
        return {"chat_id": int(f"-100{channel.id}"), "link": invite.link, "add_failed": add_failed}
    finally:
        await client.disconnect()
