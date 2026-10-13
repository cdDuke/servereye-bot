"""Регистрирует машину, на которой работает бот, как сервер администратора.

Использование: bootstrap_local.py <telegram_id> [название]
Печатает токен агента последней строкой. Повторный запуск безопасен.
"""
from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from servereye.database import repository as repo  # noqa: E402
from servereye.database.database import init_db, session_scope  # noqa: E402
from servereye.database.models import CHECK_PING  # noqa: E402
from servereye.services import access  # noqa: E402


async def main(user_id: int, name: str) -> None:
    await init_db()
    async with session_scope() as session:
        await access.ensure_owners(session)
        user = await repo.get_or_create_user(session, user_id)
        servers = await repo.list_servers(session, user.id)
        local = next((s for s in servers if s.is_local_host), None)
        if local is None:
            local = await repo.create_server(
                session, user.id, name, "127.0.0.1", CHECK_PING, agent_enabled=True
            )
            local.is_local_host = True
        if not local.agent_token:
            await repo.rotate_agent_token(session, local)
        local.agent_enabled = True
        local.agent_hostname = local.agent_hostname or socket.gethostname()
        await session.commit()
        print(local.agent_token)


if __name__ == "__main__":
    if len(sys.argv) < 2 or not sys.argv[1].isdigit():
        sys.exit("usage: bootstrap_local.py <telegram_id> [name]")
    asyncio.run(main(int(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "Домашний сервер"))
