"""Интерактивное получение строки сессии Telethon (TG_SESSION).

    docker run -it --rm velikoross/tgchanreader python -m tgchanreader.login

API_ID и API_HASH берутся на https://my.telegram.org → API development tools.
"""

from __future__ import annotations

import asyncio
import os


async def main() -> None:
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    api_id = os.environ.get("TG_API_ID") or input("TG_API_ID: ").strip()
    api_hash = os.environ.get("TG_API_HASH") or input("TG_API_HASH: ").strip()
    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.start()  # спросит телефон, код и пароль 2FA
    me = await client.get_me()
    session = client.session.save()
    await client.disconnect()
    print("\nВошли как:", getattr(me, "username", None) or me.id)
    print("\nДобавьте в .env (никому не показывайте — это доступ к аккаунту!):\n")
    print(f"TG_SESSION={session}\n")


if __name__ == "__main__":
    asyncio.run(main())
