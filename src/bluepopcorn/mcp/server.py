"""MCP server setup for BluePopcorn."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
import traceback

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations

from ..discover import discover_recommendations, find_similar
from ..enrich import enrich_results
from ..prompts import (
    MCP_ERR_GENRE_NOT_RECOGNIZED,
    MCP_ERR_INVALID_SEASONS,
    MCP_ERR_NOT_FOUND,
    MCP_ERR_RECOMMEND_EMPTY,
    MCP_ERR_RECOMMEND_NO_CRITERIA,
    MCP_ERR_SEASONS_VERIFY_FAILED,
    MCP_ERR_SEERR_API,
    MCP_ERR_SEERR_UNREACHABLE,
    MCP_ERR_SIMILAR_EMPTY,
    MCP_ERR_UNEXPECTED,
    MCP_ERR_UNKNOWN_TOOL,
    MCP_ERR_SIMILAR_NOT_FOUND,
    MCP_SERVER_INSTRUCTIONS,
    MCP_TOOL_DESC_DETAILS,
    MCP_TOOL_DESC_RECENT,
    MCP_TOOL_DESC_RECOMMEND,
    MCP_TOOL_DESC_REQUEST,
    MCP_TOOL_DESC_SEARCH,
)
from ..schemas import (
    MCP_DETAILS_SCHEMA,
    MCP_RECENT_SCHEMA,
    MCP_RECOMMEND_SCHEMA,
    MCP_REQUEST_SCHEMA,
    MCP_SEARCH_SCHEMA,
)
from ..seerr import TMDB_IMAGE_BASE, SeerrClient, SeerrConnectionError, SeerrError, parse_download_progress, seerr_title
from ..types import MediaStatus, SearchResult, status_label_for
from . import _log
from .config import Config, load_config

log = logging.getLogger(__name__)


def _result_to_dict(r: SearchResult) -> dict:
    """Convert a SearchResult to a dict for MCP output."""
    d: dict = {
        "tmdb_id": r.tmdb_id,
        "title": r.title,
        "year": r.year,
        "media_type": r.media_type,
        "overview": r.overview,
        "status": status_label_for(r.status, r.download_progress),
    }
    if r.poster_path:
        d["poster_url"] = f"{TMDB_IMAGE_BASE}{r.poster_path}"
    if r.rating:
        d["tmdb_rating"] = r.rating
    if r.rt_rating:
        d["rt_rating"] = r.rt_rating
    if r.imdb_rating:
        d["imdb_rating"] = r.imdb_rating
    if r.trailer_url:
        d["trailer_url"] = r.trailer_url
    if r.next_air_date:
        d["air_date"] = r.next_air_date
    if r.download_progress:
        d["download_progress"] = r.download_progress
    if r.season_count:
        d["season_count"] = r.season_count
    if r.collection_id:
        d["collection_id"] = r.collection_id
        d["collection_name"] = r.collection_name
    return d


def _sanitize_log(val: str, max_len: int = 200) -> str:
    """Sanitize a user-supplied string for safe logging."""
    return str(val)[:max_len].translate(str.maketrans("", "", "\r\n\t\x00"))


def _summarize_args(name: str, args: dict) -> str:
    """Summarize tool arguments for logging."""
    if name == "seerr_search":
        return f"query={_sanitize_log(args.get('query', '?'))} type={args.get('media_type', 'any')}"
    elif name == "seerr_details":
        return f"{args.get('media_type', '?')}/{args.get('tmdb_id', '?')}"
    elif name == "seerr_request":
        return f"{args.get('media_type', '?')}/{args.get('tmdb_id', '?')}"
    elif name == "seerr_recommend":
        parts = []
        if args.get("genre"):
            parts.append(f"genre={_sanitize_log(args['genre'])}")
        if args.get("keyword"):
            parts.append(f"kw={_sanitize_log(args['keyword'])}")
        if args.get("similar_to"):
            parts.append(f"similar_to={_sanitize_log(args['similar_to'])}")
        if args.get("trending"):
            parts.append("trending")
        return " ".join(parts) or "no-criteria"
    elif name == "seerr_recent":
        return f"page={args.get('page', 1)} limit={args.get('limit', 10)}"
    return str(args)[:500]


def create_server(config: Config, seerr: SeerrClient) -> Server:
    """Create and configure the MCP server with 5 Seerr tools."""
    server = Server("bluepopcorn", instructions=MCP_SERVER_INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return [
            Tool(
                name="seerr_search",
                description=MCP_TOOL_DESC_SEARCH,
                annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
                inputSchema=MCP_SEARCH_SCHEMA,
            ),
            Tool(
                name="seerr_details",
                description=MCP_TOOL_DESC_DETAILS,
                annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
                inputSchema=MCP_DETAILS_SCHEMA,
            ),
            Tool(
                name="seerr_request",
                description=MCP_TOOL_DESC_REQUEST,
                annotations=ToolAnnotations(destructiveHint=True),
                inputSchema=MCP_REQUEST_SCHEMA,
            ),
            Tool(
                name="seerr_recommend",
                description=MCP_TOOL_DESC_RECOMMEND,
                annotations=ToolAnnotations(readOnlyHint=True),
                inputSchema=MCP_RECOMMEND_SCHEMA,
            ),
            Tool(
                name="seerr_recent",
                description=MCP_TOOL_DESC_RECENT,
                annotations=ToolAnnotations(readOnlyHint=True),
                inputSchema=MCP_RECENT_SCHEMA,
            ),
        ]

    def _format_result(name: str, result: dict, start_time: float) -> CallToolResult:
        """Format a tool result into CallToolResult.

        Content errors return isError=False so parallel sibling calls
        are not cancelled by the client.
        """
        elapsed = (time.time() - start_time) * 1000
        if "error" in result:
            _log(f"TOOL END: {name} ERROR={result['error']} time={elapsed:.1f}ms")
            return CallToolResult(
                content=[TextContent(type="text", text=f"Error: {result['error']}")],
                isError=False,
            )
        content = result.get("content", "")
        _log(f"TOOL END: {name} OK content_len={len(content)} time={elapsed:.1f}ms")
        return CallToolResult(
            content=[TextContent(type="text", text=content)],
            isError=False,
        )

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> CallToolResult:
        start_time = time.time()
        _log(f"TOOL START: {name} {_summarize_args(name, arguments)}")

        try:
            if name == "seerr_search":
                return await _handle_search(arguments, start_time)
            elif name == "seerr_details":
                return await _handle_details(arguments, start_time)
            elif name == "seerr_request":
                return await _handle_request(arguments, start_time)
            elif name == "seerr_recommend":
                return await _handle_recommend(arguments, start_time)
            elif name == "seerr_recent":
                return await _handle_recent(arguments, start_time)
            else:
                elapsed = (time.time() - start_time) * 1000
                _log(f"TOOL END: {name} UNKNOWN time={elapsed:.1f}ms")
                return CallToolResult(
                    content=[TextContent(type="text", text=MCP_ERR_UNKNOWN_TOOL.format(name=name))],
                    isError=True,
                )
        except SeerrConnectionError as e:
            _log(f"TOOL END: {name} CONNECTION_ERROR={e}")
            return _format_result(name, {"error": MCP_ERR_SEERR_UNREACHABLE}, start_time)
        except SeerrError as e:
            _log(f"TOOL END: {name} SEERR_ERROR={e}")
            return _format_result(name, {"error": MCP_ERR_SEERR_API}, start_time)
        except Exception as e:
            elapsed = (time.time() - start_time) * 1000
            _log(f"TOOL END: {name} EXCEPTION={type(e).__name__}: {e} time={elapsed:.1f}ms")
            traceback.print_exc(file=sys.stderr)
            return CallToolResult(
                content=[TextContent(type="text", text=f"{type(e).__name__}: {MCP_ERR_UNEXPECTED}")],
                isError=True,
            )

    async def _handle_search(args: dict, start_time: float) -> CallToolResult:
        results = await seerr.search(args["query"], args.get("media_type"))
        await enrich_results(seerr, results)
        items = [_result_to_dict(r) for r in results]
        content = json.dumps({"results": items, "count": len(items)})
        return _format_result("seerr_search", {"content": content}, start_time)

    async def _handle_details(args: dict, start_time: float) -> CallToolResult:
        tmdb_id = args["tmdb_id"]
        media_type = args["media_type"]

        # Fetch detail (used for status + extras) and ratings concurrently
        detail_task = seerr.get_media_status(media_type, tmdb_id)
        ratings_task = seerr.get_ratings(media_type, tmdb_id)
        detail, ratings = await asyncio.gather(detail_task, ratings_task)

        if not detail:
            return _format_result("seerr_details", {"error": MCP_ERR_NOT_FOUND.format(media_type=media_type, tmdb_id=tmdb_id)}, start_time)

        title = seerr_title(detail)
        release = detail.get("releaseDate") or detail.get("firstAirDate") or ""
        year = int(release[:4]) if len(release) >= 4 else None

        d: dict = {
            "tmdb_id": tmdb_id,
            "title": title,
            "year": year,
            "media_type": media_type,
            "overview": detail.get("overview") or "",
            "tagline": detail.get("tagline") or None,
        }

        # Genres
        genres = detail.get("genres", [])
        if genres:
            d["genres"] = [g.get("name") for g in genres if g.get("name")]

        # Status (from the same detail response)
        media_info = detail.get("mediaInfo") or {}
        raw_status = media_info.get("status", 0)
        try:
            status = MediaStatus(raw_status)
        except ValueError:
            status = MediaStatus.UNKNOWN
        dl_progress = parse_download_progress(media_info) if status == MediaStatus.PROCESSING else None
        d["status"] = status_label_for(status, dl_progress)
        if dl_progress:
            d["download_progress"] = dl_progress

        # Ratings
        if ratings:
            if ratings.get("rt"):
                freshness = ratings.get("rt_freshness", "")
                d["rt_critics"] = f"{ratings['rt']} {freshness}".strip()
            if ratings.get("rt_audience"):
                d["rt_audience"] = ratings["rt_audience"]
            if ratings.get("imdb"):
                d["imdb_rating"] = ratings["imdb"]
                if ratings.get("imdb_votes"):
                    d["imdb_votes"] = ratings["imdb_votes"]
        vote_avg = detail.get("voteAverage")
        if vote_avg:
            d["tmdb_rating"] = round(vote_avg, 1)

        # Extras (extracted from the same detail response — no extra API call)
        d["trailer_url"] = SeerrClient.extract_trailer(detail)
        d["air_date"] = SeerrClient.extract_air_date(detail, media_type)
        collection = detail.get("collection")
        if isinstance(collection, dict) and collection.get("id"):
            d["collection_id"] = collection["id"]
            d["collection_name"] = collection.get("name")
        if media_type == "tv":
            seasons = detail.get("seasons", [])
            non_special = [s for s in seasons if s.get("seasonNumber", 0) > 0]
            d["season_count"] = len(non_special)
            d["seasons"] = [
                {"number": s["seasonNumber"], "episodes": s.get("episodeCount", 0)}
                for s in non_special
            ]

        # Remove None values for cleaner output
        d = {k: v for k, v in d.items() if v is not None}

        # Poster
        poster = detail.get("posterPath")
        if poster:
            d["poster_url"] = f"{TMDB_IMAGE_BASE}{poster}"

        content = json.dumps(d)
        return _format_result("seerr_details", {"content": content}, start_time)

    async def _handle_request(args: dict, start_time: float) -> CallToolResult:
        tmdb_id = args["tmdb_id"]
        media_type = args["media_type"]
        requested_seasons = args.get("seasons")

        # Pre-flight status check (dedup)
        title = f"{media_type}/{tmdb_id}"
        seasons: list[int] | None = None
        try:
            detail = await seerr.get_media_status(media_type, tmdb_id)
            if detail:
                title = seerr_title(detail)
                release = detail.get("releaseDate") or detail.get("firstAirDate") or ""
                year = release[:4] if len(release) >= 4 else ""
                if year:
                    title = f"{title} ({year})"

                # Extract seasons for TV
                if media_type == "tv":
                    all_seasons = SeerrClient.extract_season_numbers(detail)
                    if requested_seasons:
                        valid = [s for s in requested_seasons if s in all_seasons]
                        if not valid:
                            return _format_result("seerr_request", {
                                "error": MCP_ERR_INVALID_SEASONS.format(requested=requested_seasons, available=all_seasons)
                            }, start_time)
                        seasons = valid
                    else:
                        seasons = all_seasons

                media_info = detail.get("mediaInfo")
                if media_info:
                    raw_status = media_info.get("status", 0)
                    try:
                        status = MediaStatus(raw_status)
                    except ValueError:
                        status = MediaStatus.UNKNOWN
                    if status in (MediaStatus.AVAILABLE, MediaStatus.PARTIALLY_AVAILABLE,
                                  MediaStatus.PROCESSING, MediaStatus.PENDING):
                        d = {
                            "already_exists": True,
                            "title": title,
                            "status": status_label_for(status),
                            "tmdb_id": tmdb_id,
                            "media_type": media_type,
                        }
                        content = json.dumps(d)
                        return _format_result("seerr_request", {"content": content}, start_time)
        except SeerrConnectionError:
            raise  # Connection failed — don't attempt the request
        except Exception as e:
            _log(f"Pre-request status check failed: {e}")
            if requested_seasons:
                return _format_result("seerr_request", {
                    "error": MCP_ERR_SEASONS_VERIFY_FAILED
                }, start_time)

        # Make the request
        result = await seerr.request_media(media_type, tmdb_id, seasons=seasons)
        d = {
            "requested": True,
            "title": title,
            "tmdb_id": tmdb_id,
            "media_type": media_type,
            "request_id": result.get("id"),
        }
        content = json.dumps(d)
        return _format_result("seerr_request", {"content": content}, start_time)

    async def _handle_recommend(args: dict, start_time: float) -> CallToolResult:
        similar_to = args.get("similar_to")
        take = min(args.get("count") or 5, 10)
        exclude = set(args.get("exclude_ids") or [])

        if similar_to:
            results, base_title = await find_similar(
                seerr, similar_to,
                media_type=args.get("media_type"),
                exclude_ids=exclude,
                take=take,
            )
            if not results:
                msg = MCP_ERR_SIMILAR_EMPTY.format(title=similar_to)
                if base_title is None:
                    msg = MCP_ERR_SIMILAR_NOT_FOUND.format(title=similar_to)
                return _format_result("seerr_recommend", {"error": msg}, start_time)
            await enrich_results(seerr, results)
            items = [_result_to_dict(r) for r in results]
            content = json.dumps({
                "similar_to": base_title,
                "results": items,
                "count": len(items),
            })
            return _format_result("seerr_recommend", {"content": content}, start_time)

        # Validate at least one criterion is provided
        if not any((args.get("genre"), args.get("keyword"), args.get("trending"),
                     args.get("upcoming"), args.get("year"), args.get("year_end"))):
            return _format_result("seerr_recommend", {
                "error": MCP_ERR_RECOMMEND_NO_CRITERIA
            }, start_time)

        results, available_genres = await discover_recommendations(
            seerr,
            genre=args.get("genre"),
            keyword=args.get("keyword"),
            media_type=args.get("media_type"),
            year=args.get("year"),
            year_end=args.get("year_end"),
            trending=args.get("trending", False),
            upcoming=args.get("upcoming", False),
            take=take,
            exclude_ids=exclude,
        )

        if not results:
            if available_genres:
                genres_str = ", ".join(available_genres)
                return _format_result("seerr_recommend", {
                    "error": MCP_ERR_GENRE_NOT_RECOGNIZED.format(genre=args.get("genre"), available=genres_str)
                }, start_time)
            return _format_result("seerr_recommend", {"error": MCP_ERR_RECOMMEND_EMPTY}, start_time)

        await enrich_results(seerr, results)
        items = [_result_to_dict(r) for r in results]
        content = json.dumps({"results": items, "count": len(items)})
        return _format_result("seerr_recommend", {"content": content}, start_time)

    async def _handle_recent(args: dict, start_time: float) -> CallToolResult:
        page = args.get("page", 1)
        limit = min(args.get("limit") or 10, 20)
        data = await seerr.get_server_state(page=page, take=limit)

        available = data.get("available", [])
        requested = data.get("requested", [])

        result: dict = {"page": page, "available": [], "requested": []}

        if available:
            result["available"] = [
                {
                    "title": item["title"],
                    "year": item.get("year"),
                    "media_type": item["media_type"],
                    "tmdb_id": item["tmdb_id"],
                    "status": status_label_for(item["status"]),
                    "added_at": item.get("added_at", ""),
                }
                for item in available
            ]

        if requested:
            result["requested"] = [
                {
                    "title": item["title"],
                    "year": item.get("year"),
                    "media_type": item["media_type"],
                    "tmdb_id": item["tmdb_id"],
                    "status": status_label_for(item["status"]),
                    "requested_at": item.get("requested_at", ""),
                }
                for item in requested
            ]

        content = json.dumps(result)
        return _format_result("seerr_recent", {"content": content}, start_time)

    return server


async def run_stdio_server(config: Config | None = None) -> None:
    """Run the server in stdio mode."""
    if config is None:
        config = load_config()

    seerr = SeerrClient(
        base_url=config.seerr_url,
        api_key=config.seerr_api_key,
        timeout=config.http_timeout,
    )

    server = create_server(config, seerr)

    _log("bluepopcorn MCP stdio server started")

    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream,
                server.create_initialization_options(),
            )
    finally:
        await seerr.close()
