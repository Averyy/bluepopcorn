from __future__ import annotations

import asyncio
import logging
import sys

from .actions import ActionExecutor
from .config import Settings, load_settings
from .db import BotDatabase
from .llm import LLMClient
from .seerr import SeerrClient

log = logging.getLogger(__name__)

CLI_SENDER = "cli-user"


async def cli_mode() -> None:
    """Interactive CLI test mode. Bypasses iMessage, uses stdin/stdout."""
    settings = load_settings()
    seerr = SeerrClient(settings)
    llm = LLMClient(settings)
    db = BotDatabase(settings)

    await db.init()

    try:
        await seerr.authenticate()
    except Exception as e:
        print(f"Warning: Seerr auth failed ({e}), search/request won't work")

    executor = ActionExecutor(
        seerr=seerr,
        llm=llm,
        sender=None,  # No iMessage in CLI mode
        posters=None,  # No poster sending in CLI mode
        db=db,
        settings=settings,
    )

    # Clear CLI user history on start
    await db.clear_history(CLI_SENDER)

    print("iMessagarr CLI mode. Type messages, Ctrl+C to quit.\n")

    try:
        while True:
            try:
                text = input("You: ").strip()
            except EOFError:
                break

            if not text:
                continue

            response = await executor.handle_message(CLI_SENDER, text)
            print(f"Bot: {response}\n")

    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        await db.close()
        await seerr.close()
