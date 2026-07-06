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


def _http_response(status_line: str, body: str) -> bytes:
    """Build a minimal HTTP/1.1 response with a correct Content-Length."""
    payload = body.encode()
    head = f"HTTP/1.1 {status_line}\r\nContent-Length: {len(payload)}\r\n\r\n"
    return head.encode() + payload


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
            on_notification: Async callback(message, target_phone) -> bool.
                             target_phone is None for broadcast. Returns
                             True when delivered or queued for later.
            request_tracker: Optional tracker for targeted notifications.
        """
        self.port = settings.webhook_port
        self._secret = settings.webhook_secret
        self._allowed_host = urlparse(settings.seerr_url).hostname or ""
        # Resolved at start() — comparing the peer IP against the URL
        # *hostname* string would reject every connection for DNS names.
        self._allowed_ips: set[str] = set()
        self.on_notification = on_notification
        self._tracker = request_tracker
        self._server: asyncio.Server | None = None
        # In-flight notification tasks (webhooks are answered before the
        # potentially slow iMessage send runs)
        self._tasks: set[asyncio.Task] = set()

    # Maximum allowed request body size (1 MB)
    MAX_BODY_SIZE = 1 * 1024 * 1024

    # Connection timeout in seconds
    CONNECTION_TIMEOUT = 30

    async def start(self) -> None:
        if self._allowed_host:
            try:
                infos = await asyncio.get_running_loop().getaddrinfo(self._allowed_host, None)
                self._allowed_ips = {info[4][0] for info in infos}
            except OSError as e:
                log.error(
                    "Could not resolve Seerr host %r for webhook IP filtering: %s "
                    "— fix SEERR_URL or set WEBHOOK_SECRET in .env",
                    self._allowed_host, e,
                )
        if not self._secret and not self._allowed_ips:
            log.error(
                "Webhook server has NO protection: WEBHOOK_SECRET is unset and the "
                "Seerr host could not be resolved for IP filtering. All webhook "
                "connections will be REJECTED until one is fixed — set "
                "WEBHOOK_SECRET in .env (recommended) or a resolvable SEERR_URL."
            )
        elif not self._secret:
            log.warning(
                "WEBHOOK_SECRET not set — relying on IP filtering only (allowed: %s). "
                "Set WEBHOOK_SECRET in .env for stronger protection.",
                sorted(self._allowed_ips),
            )
        self._server = await asyncio.start_server(
            self._handle_connection_wrapper, "0.0.0.0", self.port
        )
        log.info(
            "Webhook server listening on port %d (allowed IPs: %s)",
            self.port, sorted(self._allowed_ips) or "secret-only",
        )

    async def _handle_connection_wrapper(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        peer_ip = peer[0] if peer else ""
        if self._allowed_ips:
            if peer_ip not in self._allowed_ips:
                log.warning(
                    "Rejected webhook connection from %s (allowed: %s)",
                    peer_ip, sorted(self._allowed_ips),
                )
                writer.close()
                await writer.wait_closed()
                return
        elif not self._secret:
            # No usable protection at all — fail closed rather than let any
            # LAN host make the bot text arbitrary strings.
            log.warning("Rejected webhook connection from %s (no secret, no IP filter)", peer_ip)
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
                writer.write(_http_response("400 Bad Request", "Bad Content-Length"))
                await writer.drain()
                return

            if content_length > self.MAX_BODY_SIZE:
                writer.write(_http_response("413 Payload Too Large", "Payload Too Large"))
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
                    response = _http_response("403 Forbidden", "Unauthorized")
                else:
                    # Respond before the (slow) iMessage send — retries and
                    # a Messages restart can exceed Seerr's delivery timeout
                    # and trigger duplicate webhook deliveries.
                    self._spawn(self._handle_webhook(body))
                    response = _http_response("200 OK", "OK")
            else:
                response = _http_response("404 Not Found", "Not Found")

            writer.write(response)
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

        # For available/failed: target the requester specifically. The
        # tracker entry is only removed after delivery is confirmed (or
        # queued) — removing first loses the notification forever when the
        # send fails or falls in quiet hours.
        tmdb_id = media.get("tmdbId")
        media_type = media.get("mediaType")
        if notification_type in ("MEDIA_AVAILABLE", "MEDIA_FAILED") and self._tracker and tmdb_id and media_type:
            phones = await self._tracker.lookup(media_type, tmdb_id)
            if phones:
                delivered = True
                for phone in phones:
                    delivered = bool(await self.on_notification(text, phone)) and delivered
                if delivered:
                    await self._tracker.remove(media_type, tmdb_id)
                else:
                    log.warning(
                        "Notification for %s tmdb:%s not delivered — keeping tracker entry",
                        media_type, tmdb_id,
                    )
                return
            await self._tracker.remove(media_type, tmdb_id)

        # Broadcast (approved, pending, or no tracker match)
        await self.on_notification(text, None)

    def _spawn(self, coro: Coroutine) -> None:
        """Run a webhook handler in the background, logging any failure."""
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def _done(t: asyncio.Task) -> None:
            self._tasks.discard(t)
            if not t.cancelled() and t.exception():
                log.error("Webhook notification task failed: %s", t.exception())

        task.add_done_callback(_done)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        # Let in-flight notification sends finish before shutdown
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        if self._server:
            log.info("Webhook server stopped")
