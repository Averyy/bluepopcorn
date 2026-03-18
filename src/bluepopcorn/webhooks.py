from __future__ import annotations

import asyncio
import json
import logging
from typing import Callable, Coroutine

from .config import Settings

log = logging.getLogger(__name__)


class WebhookServer:
    def __init__(
        self,
        settings: Settings,
        on_notification: Callable[[str], Coroutine],
    ) -> None:
        """
        Args:
            settings: App settings.
            on_notification: Async callback that receives a formatted message
                             string to send to all allowed senders.
        """
        self.port = settings.webhook_port
        self.on_notification = on_notification
        self._server: asyncio.Server | None = None

    # Maximum allowed request body size (1 MB)
    MAX_BODY_SIZE = 1 * 1024 * 1024

    # Connection timeout in seconds
    CONNECTION_TIMEOUT = 30

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection_wrapper, "127.0.0.1", self.port
        )
        log.info("Webhook server listening on port %d", self.port)

    async def _handle_connection_wrapper(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await asyncio.wait_for(
                self._handle_connection(reader, writer),
                timeout=self.CONNECTION_TIMEOUT,
            )
        except asyncio.TimeoutError:
            log.warning("Webhook connection timed out")
            writer.close()
            await writer.wait_closed()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            # Read HTTP request
            request_line = await reader.readline()
            headers: dict[str, str] = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                decoded = line.decode("utf-8", errors="ignore").strip()
                if ":" in decoded:
                    key, val = decoded.split(":", 1)
                    headers[key.strip().lower()] = val.strip()

            # Read body
            try:
                content_length = int(headers.get("content-length", "0"))
            except ValueError:
                response = "HTTP/1.1 400 Bad Request\r\nContent-Length: 22\r\n\r\nBad Content-Length"
                writer.write(response.encode())
                await writer.drain()
                return

            if content_length > self.MAX_BODY_SIZE:
                response = "HTTP/1.1 413 Payload Too Large\r\nContent-Length: 16\r\n\r\nPayload Too Large"
                writer.write(response.encode())
                await writer.drain()
                return

            body = b""
            if content_length > 0:
                body = await reader.readexactly(content_length)

            # Parse request
            method_path = request_line.decode("utf-8", errors="ignore").strip()
            parts = method_path.split()
            method = parts[0] if parts else ""
            path = parts[1] if len(parts) > 1 else ""

            if method == "POST" and path == "/webhook":
                await self._handle_webhook(body)
                response = "HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK"
            else:
                response = "HTTP/1.1 404 Not Found\r\nContent-Length: 9\r\n\r\nNot Found"

            writer.write(response.encode())
            await writer.drain()
        except Exception as e:
            log.error("Webhook handler error: %s", e)
        finally:
            writer.close()
            await writer.wait_closed()

    async def _handle_webhook(self, body: bytes) -> None:
        """Parse Seerr webhook payload and send notification."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            log.error("Invalid JSON in webhook body")
            return

        notification_type = data.get("notification_type", "")
        subject = data.get("subject", "")
        message = data.get("message", "")
        media = data.get("media", {})
        title = media.get("tmdbTitle") or subject

        log.info("Webhook received: type=%s, title=%s", notification_type, title)

        # Format notification based on type
        if notification_type == "MEDIA_APPROVED":
            text = f"{title} has been approved and is being downloaded."
        elif notification_type == "MEDIA_AVAILABLE":
            text = f"{title} is now available to watch!"
        elif notification_type == "MEDIA_FAILED":
            text = f"Failed to download {title}. Someone should check Seerr."
        elif notification_type == "MEDIA_PENDING":
            text = f"New request: {title} is pending approval."
        else:
            text = f"Seerr: {subject}" if subject else None

        if text:
            await self.on_notification(text)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("Webhook server stopped")
