from __future__ import annotations

import asyncio
import hmac
import json
import logging
from typing import Callable, Coroutine
from urllib.parse import urlparse

from string import Template

from .config import Settings
from .prompts import (
    WEBHOOK_MEDIA_APPROVED,
    WEBHOOK_MEDIA_AVAILABLE,
    WEBHOOK_MEDIA_FAILED,
    WEBHOOK_MEDIA_PENDING,
    WEBHOOK_FALLBACK,
)
from .request_tracker import RequestTracker

log = logging.getLogger(__name__)


class WebhookServer:
    def __init__(
        self,
        settings: Settings,
        on_notification: Callable[[str, str | None], Coroutine],
        request_tracker: RequestTracker | None = None,
    ) -> None:
        """
        Args:
            settings: App settings.
            on_notification: Async callback(message, target_phone).
                             target_phone is None for broadcast.
            request_tracker: Optional tracker for targeted notifications.
        """
        self.port = settings.webhook_port
        self._secret = settings.webhook_secret
        self._allowed_ip = urlparse(settings.seerr_url).hostname or ""
        self.on_notification = on_notification
        self._tracker = request_tracker
        self._server: asyncio.Server | None = None

    # Maximum allowed request body size (1 MB)
    MAX_BODY_SIZE = 1 * 1024 * 1024

    # Connection timeout in seconds
    CONNECTION_TIMEOUT = 30

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection_wrapper, "0.0.0.0", self.port
        )
        if not self._secret:
            log.info(
                "WEBHOOK_SECRET not set — relying on IP filtering (Seerr: %s)",
                self._allowed_ip,
            )
        log.info("Webhook server listening on port %d (allowed: %s)", self.port, self._allowed_ip)

    async def _handle_connection_wrapper(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else ""
        if self._allowed_ip and peer_ip != self._allowed_ip:
            log.warning("Rejected webhook connection from %s (expected %s)", peer_ip, self._allowed_ip)
            writer.close()
            await writer.wait_closed()
            return
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
                if self._secret and not self._verify_signature(
                    headers, body
                ):
                    response = "HTTP/1.1 403 Forbidden\r\nContent-Length: 12\r\n\r\nUnauthorized"
                else:
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

    def _verify_signature(
        self, headers: dict[str, str], body: bytes
    ) -> bool:
        """Validate the webhook secret.

        Seerr sends the secret as a raw ``Authorization`` header value.
        """
        auth = headers.get("authorization", "")
        return hmac.compare_digest(auth, self._secret)

    async def _handle_webhook(self, body: bytes) -> None:
        """Parse Seerr webhook payload and send notification."""
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            log.error("Invalid JSON in webhook body")
            return

        notification_type = data.get("notification_type", "")
        subject = data.get("subject", "")
        media = data.get("media", {})
        title = media.get("tmdbTitle") or subject

        log.info("Webhook received: type=%s, title=%s", notification_type, title)

        # Format notification text (templates live in prompts.py)
        templates = {
            "MEDIA_APPROVED": WEBHOOK_MEDIA_APPROVED,
            "MEDIA_AVAILABLE": WEBHOOK_MEDIA_AVAILABLE,
            "MEDIA_FAILED": WEBHOOK_MEDIA_FAILED,
            "MEDIA_PENDING": WEBHOOK_MEDIA_PENDING,
        }
        template = templates.get(notification_type)
        if template:
            text = Template(template).safe_substitute(title=title)
        elif subject:
            text = Template(WEBHOOK_FALLBACK).safe_substitute(subject=subject)
        else:
            text = None

        if not text:
            return

        # For available/failed: target the requester specifically, clean up tracker
        tmdb_id = media.get("tmdbId")
        media_type = media.get("mediaType")
        if notification_type in ("MEDIA_AVAILABLE", "MEDIA_FAILED") and self._tracker and tmdb_id and media_type:
            phones = await self._tracker.lookup(media_type, tmdb_id)
            await self._tracker.remove(media_type, tmdb_id)
            if phones:
                for phone in phones:
                    await self.on_notification(text, phone)
                return

        # Broadcast (approved, pending, or no tracker match)
        await self.on_notification(text, None)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            log.info("Webhook server stopped")
