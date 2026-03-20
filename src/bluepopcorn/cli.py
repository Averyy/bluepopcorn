from __future__ import annotations

import asyncio
import logging
import sys

from .actions import ActionExecutor
from .config import Settings, load_settings
from .llm import LLMClient
from .memory import UserMemory
from .seerr import SeerrClient

log = logging.getLogger(__name__)

CLI_SENDER = "cli-user"


async def cli_mode() -> None:
    """Interactive CLI test mode. Bypasses iMessage, uses stdin/stdout."""
    settings = load_settings()
    seerr = SeerrClient(settings)
    llm = LLMClient(settings)
    memory = UserMemory(settings)

    executor = ActionExecutor(
        seerr=seerr,
        llm=llm,
        sender=None,  # No iMessage in CLI mode
        posters=None,  # No poster sending in CLI mode
        memory=memory,
        monitor=None,  # Signals CLI mode — use _cli_history
        settings=settings,
    )

    print("BluePopcorn CLI mode. Type messages, Ctrl+C to quit.\n")

    try:
        while True:
            try:
                text = input("You: ").strip()
            except EOFError:
                break

            if not text:
                continue

            log.info("CLI input: %s", text)
            response = await executor.handle_message(CLI_SENDER, text)
            print(f"Bot: {response}\n")

    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        await seerr.close()
