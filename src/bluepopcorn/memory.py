"""Per-user markdown memory manager.

Stores user profiles, preferences, and conversation summaries in per-user
markdown files under data/memory/{phone}.md. All I/O is synchronous
(files are tiny). Writes use atomic rename for crash safety.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from .config import Settings
from .utils import mask_phone

log = logging.getLogger(__name__)

SECTION_ORDER = ("Profile", "Preferences", "Likes", "Dislikes", "Recent", "Weekly", "History")

_NAME_RE = re.compile(
    r"(?:my name is|i'm|i am|call me|(?:\S+ )?name is)\s+(.+)", re.IGNORECASE,
)


def _sanitize_for_memory(text: str) -> str:
    """Strip angle brackets to prevent prompt injection via stored memory."""
    return text.replace("<", "").replace(">", "")


class UserMemory:
    def __init__(self, settings: Settings) -> None:
        self.memory_dir = settings.resolve_path(settings.memory_dir)
        self._locks: dict[str, asyncio.Lock] = {}

    def get_lock(self, sender: str) -> asyncio.Lock:
        """Get a per-sender lock for safe concurrent access."""
        if sender not in self._locks:
            self._locks[sender] = asyncio.Lock()
        return self._locks[sender]

    def _path(self, sender: str) -> Path:
        path = self.memory_dir / f"{sender}.md"
        if not path.resolve().is_relative_to(self.memory_dir.resolve()):
            raise ValueError(f"Invalid sender for memory path: {sender!r}")
        return path

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write to temp file then rename (atomic on POSIX)."""
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = path.with_suffix(".tmp")
        try:
            tmp.write_text(content)
            tmp.chmod(0o600)
            tmp.rename(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    # ── Parsing / rebuilding ─────────────────────────────────────

    def parse_sections(self, content: str) -> dict[str, list[str]]:
        """Parse markdown into {section_name: [lines]} dict."""
        sections: dict[str, list[str]] = {}
        current: str | None = None
        for line in content.splitlines():
            if line.startswith("# "):
                current = line[2:].strip()
                sections.setdefault(current, [])
            elif current is not None:
                sections[current].append(line)
        return sections

    def _rebuild(self, sections: dict[str, list[str]]) -> str:
        """Rebuild markdown from sections dict, in canonical order."""
        parts: list[str] = []
        for name in SECTION_ORDER:
            parts.append(f"# {name}")
            lines = sections.get(name, [])
            content = [ln for ln in lines if ln.strip()]
            parts.extend(content)
            parts.append("")  # blank line between sections
        # Preserve any unknown sections (e.g. manually added)
        for name, lines in sections.items():
            if name not in SECTION_ORDER:
                parts.append(f"# {name}")
                content = [ln for ln in lines if ln.strip()]
                parts.extend(content)
                parts.append("")
        return "\n".join(parts).rstrip() + "\n"

    # ── Public API ───────────────────────────────────────────────

    def load(self, sender: str) -> str:
        """Read the full markdown file. Empty string if no file."""
        path = self._path(sender)
        if path.exists():
            return path.read_text()
        return ""

    def load_or_create(self, sender: str) -> str:
        """Create file with empty sections if it doesn't exist."""
        path = self._path(sender)
        if path.exists():
            return path.read_text()
        content = self._rebuild({})
        self._atomic_write(path, content)
        return content

    def get_profile(self, sender: str) -> dict[str, str]:
        """Parse # Profile as a dict of key: value pairs."""
        content = self.load(sender)
        if not content:
            return {}
        sections = self.parse_sections(content)
        profile: dict[str, str] = {}
        for line in sections.get("Profile", []):
            if ":" in line and line.strip():
                key, _, value = line.partition(":")
                profile[key.strip().lower()] = value.strip()
        return profile

    def set_profile_field(self, sender: str, key: str, value: str) -> None:
        """Set a field in # Profile."""
        value = _sanitize_for_memory(value)
        content = self.load_or_create(sender)
        sections = self.parse_sections(content)
        profile_lines = sections.get("Profile", [])

        target = f"{key.lower()}:"
        found = False
        for i, line in enumerate(profile_lines):
            if line.strip().lower().startswith(target):
                profile_lines[i] = f"{key.capitalize()}: {value}"
                found = True
                break
        if not found:
            profile_lines.append(f"{key.capitalize()}: {value}")

        sections["Profile"] = profile_lines
        self._atomic_write(self._path(sender), self._rebuild(sections))
        log.info("Set %s=%s for %s", key, value, mask_phone(sender))

    def get_preferences(self, sender: str) -> list[str]:
        """Return # Preferences entries (without the '- ' prefix)."""
        content = self.load(sender)
        if not content:
            return []
        sections = self.parse_sections(content)
        return [
            line[2:] for line in sections.get("Preferences", [])
            if line.startswith("- ")
        ]

    def add_preference(self, sender: str, fact: str) -> bool:
        """Append to # Preferences with fuzzy dedup.

        Name-like facts ("my name is X", "I'm X", "call me X") route
        to set_profile_field instead.

        Returns True if the fact was stored (or routed to profile), False if duplicate.
        """
        fact = _sanitize_for_memory(fact)

        name_match = _NAME_RE.match(fact)
        if name_match:
            name = name_match.group(1).strip().rstrip(".")
            self.set_profile_field(sender, "Name", name)
            return True

        content = self.load_or_create(sender)
        sections = self.parse_sections(content)
        prefs = sections.get("Preferences", [])

        # Fuzzy dedup — skip if new fact is substring of existing OR vice versa
        fact_lower = fact.lower()
        for line in prefs:
            if line.startswith("- "):
                existing_lower = line[2:].lower()
                if fact_lower in existing_lower or existing_lower in fact_lower:
                    log.debug("Duplicate preference skipped for %s: %s", mask_phone(sender), fact[:60])
                    return False

        prefs.append(f"- {fact}")
        sections["Preferences"] = prefs
        self._atomic_write(self._path(sender), self._rebuild(sections))
        log.info("Added preference for %s: %s", mask_phone(sender), fact[:80])
        return True

    def parse_tastes(self, sections: dict[str, list[str]]) -> dict[str, list[str]]:
        """Extract tastes from parsed sections dict."""
        tastes: dict[str, list[str]] = {}
        for line in sections.get("Likes", []):
            if ":" in line and line.strip():
                key, _, value = line.partition(":")
                items = [v.strip() for v in value.split(",") if v.strip()]
                if items:
                    tastes[key.strip().lower()] = items
        for line in sections.get("Dislikes", []):
            if ":" in line and line.strip():
                key, _, value = line.partition(":")
                items = [v.strip() for v in value.split(",") if v.strip()]
                if items:
                    tastes[f"avoid {key.strip().lower()}"] = items
        return tastes

    def get_tastes(self, sender: str) -> dict[str, list[str]]:
        """Parse # Likes and # Dislikes as {category: [items]} dict."""
        content = self.load(sender)
        if not content:
            return {}
        return self.parse_tastes(self.parse_sections(content))

    def update_tastes(
        self,
        sender: str,
        genres: list[str] | None = None,
        movies: list[str] | None = None,
        shows: list[str] | None = None,
        avoid_genres: list[str] | None = None,
        avoid: list[str] | None = None,
    ) -> None:
        """Merge new taste items into # Likes and # Dislikes (case-insensitive dedup)."""
        # Sanitize all incoming items
        genres = [_sanitize_for_memory(g) for g in genres] if genres else genres
        movies = [_sanitize_for_memory(m) for m in movies] if movies else movies
        shows = [_sanitize_for_memory(s) for s in shows] if shows else shows
        avoid_genres = [_sanitize_for_memory(g) for g in avoid_genres] if avoid_genres else avoid_genres
        avoid = [_sanitize_for_memory(a) for a in avoid] if avoid else avoid
        content = self.load_or_create(sender)
        sections = self.parse_sections(content)
        existing = self.parse_tastes(sections)

        def _merge(key: str, new_items: list[str] | None) -> str | None:
            if not new_items:
                return None
            old = existing.get(key, [])
            old_lower = {item.lower() for item in old}
            merged = list(old)
            for item in new_items:
                if item.strip() and item.strip().lower() not in old_lower:
                    merged.append(item.strip())
                    old_lower.add(item.strip().lower())
            return ", ".join(merged) if merged else None

        # Build Likes section
        likes_lines: list[str] = []
        for key, label, items in [
            ("genres", "Genres", genres),
            ("movies", "Movies", movies),
            ("shows", "Shows", shows),
        ]:
            result = _merge(key, items)
            if result:
                likes_lines.append(f"{label}: {result}")
            elif key in existing:
                likes_lines.append(f"{label}: {', '.join(existing[key])}")

        # Build Dislikes section
        dislikes_lines: list[str] = []
        for key, label, items in [
            ("avoid genres", "Genres", avoid_genres),
            ("avoid titles", "Titles", avoid),
        ]:
            result = _merge(key, items)
            if result:
                dislikes_lines.append(f"{label}: {result}")
            elif key in existing:
                dislikes_lines.append(f"{label}: {', '.join(existing[key])}")

        sections["Likes"] = likes_lines
        sections["Dislikes"] = dislikes_lines
        self._atomic_write(self._path(sender), self._rebuild(sections))
        all_items = [genres, movies, shows, avoid_genres, avoid]
        added = sum(len(x) for x in all_items if x)
        if added:
            log.info("Updated tastes for %s (+%d items)", mask_phone(sender), added)

    def append_summary(
        self, sender: str, date: str, summary: str, tier: str = "Recent"
    ) -> None:
        """Append a dated summary to # Recent, # Weekly, or # History."""
        summary = _sanitize_for_memory(summary)
        content = self.load_or_create(sender)
        sections = self.parse_sections(content)
        section_lines = sections.get(tier, [])
        section_lines.append(f"- {date}: {summary}")
        sections[tier] = section_lines
        self._atomic_write(self._path(sender), self._rebuild(sections))

    def get_section(self, sender: str, section: str) -> list[str]:
        """Return content lines from a specific section."""
        content = self.load(sender)
        if not content:
            return []
        return self.parse_sections(content).get(section, [])

    def replace_section(self, sender: str, section: str, lines: list[str]) -> None:
        """Replace all lines in a section (used by compression)."""
        lines = [_sanitize_for_memory(ln) for ln in lines]
        content = self.load_or_create(sender)
        sections = self.parse_sections(content)
        sections[section] = lines
        self._atomic_write(self._path(sender), self._rebuild(sections))

    def save(self, sender: str, sections: dict[str, list[str]]) -> None:
        """Write sections dict to the memory file."""
        self._atomic_write(self._path(sender), self._rebuild(sections))

    # ── In-place section manipulation (no I/O, for batch operations) ──

    def append_summary_to(
        self, sections: dict[str, list[str]], date: str, summary: str, tier: str = "Recent"
    ) -> None:
        """Append a dated summary to a sections dict (no I/O)."""
        summary = _sanitize_for_memory(summary)
        sections.setdefault(tier, []).append(f"- {date}: {summary}")

    def update_tastes_in(
        self,
        sections: dict[str, list[str]],
        *,
        genres: list[str] | None = None,
        movies: list[str] | None = None,
        shows: list[str] | None = None,
        avoid_genres: list[str] | None = None,
        avoid: list[str] | None = None,
    ) -> int:
        """Merge new taste items into sections dict (no I/O). Returns count added."""
        genres = [_sanitize_for_memory(g) for g in genres] if genres else genres
        movies = [_sanitize_for_memory(m) for m in movies] if movies else movies
        shows = [_sanitize_for_memory(s) for s in shows] if shows else shows
        avoid_genres = [_sanitize_for_memory(g) for g in avoid_genres] if avoid_genres else avoid_genres
        avoid = [_sanitize_for_memory(a) for a in avoid] if avoid else avoid

        existing = self.parse_tastes(sections)

        def _merge(key: str, new_items: list[str] | None) -> str | None:
            if not new_items:
                return None
            old = existing.get(key, [])
            old_lower = {item.lower() for item in old}
            merged = list(old)
            for item in new_items:
                if item.strip() and item.strip().lower() not in old_lower:
                    merged.append(item.strip())
                    old_lower.add(item.strip().lower())
            return ", ".join(merged) if merged else None

        likes_lines: list[str] = []
        for key, label, items in [
            ("genres", "Genres", genres),
            ("movies", "Movies", movies),
            ("shows", "Shows", shows),
        ]:
            result = _merge(key, items)
            if result:
                likes_lines.append(f"{label}: {result}")
            elif key in existing:
                likes_lines.append(f"{label}: {', '.join(existing[key])}")

        dislikes_lines: list[str] = []
        for key, label, items in [
            ("avoid genres", "Genres", avoid_genres),
            ("avoid titles", "Titles", avoid),
        ]:
            result = _merge(key, items)
            if result:
                dislikes_lines.append(f"{label}: {result}")
            elif key in existing:
                dislikes_lines.append(f"{label}: {', '.join(existing[key])}")

        sections["Likes"] = likes_lines
        sections["Dislikes"] = dislikes_lines
        all_items = [genres, movies, shows, avoid_genres, avoid]
        return sum(len(x) for x in all_items if x)

    def add_preference_to(
        self, sections: dict[str, list[str]], fact: str
    ) -> bool:
        """Add preference to sections dict (no I/O). Returns True if added."""
        fact = _sanitize_for_memory(fact)

        name_match = _NAME_RE.match(fact)
        if name_match:
            name = name_match.group(1).strip().rstrip(".")
            profile_lines = sections.setdefault("Profile", [])
            target = "name:"
            found = False
            for i, line in enumerate(profile_lines):
                if line.strip().lower().startswith(target):
                    profile_lines[i] = f"Name: {name}"
                    found = True
                    break
            if not found:
                profile_lines.append(f"Name: {name}")
            return True

        prefs = sections.setdefault("Preferences", [])
        fact_lower = fact.lower()
        for line in prefs:
            if line.startswith("- "):
                existing_lower = line[2:].lower()
                if fact_lower in existing_lower or existing_lower in fact_lower:
                    return False
        prefs.append(f"- {fact}")
        return True

    def truncate_if_needed(self, sender: str, max_lines: int = 200) -> None:
        """Trim oldest entries from History, then Weekly, then Recent."""
        path = self._path(sender)
        if not path.exists():
            return
        content = path.read_text()
        if len(content.splitlines()) <= max_lines:
            return

        sections = self.parse_sections(content)

        # Count how many lines to shed
        rebuilt = self._rebuild(sections)
        excess = len(rebuilt.splitlines()) - max_lines

        for tier in ("History", "Weekly", "Recent"):
            if excess <= 0:
                break
            tier_lines = sections.get(tier, [])
            while excess > 0:
                # Find and remove the oldest (first) bullet entry
                removed = False
                for i, line in enumerate(tier_lines):
                    if line.startswith("- "):
                        tier_lines.pop(i)
                        excess -= 1
                        removed = True
                        break
                if not removed:
                    break  # No bullets left in this tier
            sections[tier] = tier_lines

        self._atomic_write(path, self._rebuild(sections))
        log.info("Truncated memory for %s to ≤%d lines", mask_phone(sender), max_lines)
