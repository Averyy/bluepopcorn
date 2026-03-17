# Contacts Integration: Know Who You're Talking To

## Context

Currently the bot only responds to phone numbers hardcoded in `ALLOWED_SENDERS`. It doesn't know who anyone is — just raw phone numbers. Goals:

1. **Anyone in macOS Contacts can message the bot** (replaces hardcoded `ALLOWED_SENDERS`)
2. **The bot knows who it's talking to** — name, nickname, birthday, notes from the contact card get injected into the LLM prompt so responses are personalized

**Current flow:** Incoming phone → check against `ALLOWED_SENDERS` → respond. Bot has zero idea who the person is.

**Goal:** Incoming phone → instant dict lookup against contacts loaded at startup → if found, allow + inject contact info into prompt. Bot knows it's talking to "Avery" who "likes sci-fi and has a birthday on March 5th".

## Design

### Bulk Load, Not Per-Message Queries

On startup (and every 5 minutes), run ONE AppleScript that dumps all contacts with phone numbers. Build a `normalized_phone → ContactInfo` dict in memory. Every inbound filter check and prompt enrichment is then an instant dict lookup — no per-message AppleScript.

### Inbound Filter

```
if normalized_phone in contacts_dict → allow (with contact enrichment)
elif phone in ALLOWED_SENDERS        → allow (no enrichment, fallback)
else                                 → ignore
```

`ALLOWED_SENDERS` in `.env` is the fallback if Contacts permission isn't granted or for edge-case numbers not in the address book. When contacts access works, it's the primary allow list.

### Contact Info in the Prompt

```
<contact>
Name: Avery
Nickname: Av
Birthday: March 5
Notes: Prefers 4K quality. Watches a lot of sci-fi.
</contact>
```

Injected after the `<context>` time tag in `_build_prompt()`. Only fields with values. Contact notes complement `remember`/`forget` facts — notes are set in Contacts.app (bot can't modify), facts are what the user tells the bot.

## Prior Art: OpenClaw

**apple-contacts skill** (community, by tyler6204): Validates the AppleScript approach. Key gotchas:
- **`first person whose` is buggy** — always use `every person whose` + `item 1 of matches`
- **Phone matching is exact string** — `+15551234567` won't match `5551234567`. We sidestep this by normalizing both sides at load time

**carddav-contacts skill**: vdirsyncer + khard for CardDAV sync. Overkill for macOS-only.

**DM access model**: OpenClaw uses manual pairing codes, not address book lookups. Our contacts-as-allow-list is simpler for a personal bot.

**USER.md template**: Per-user profile learned from conversation over time. They do NOT pull contact card info from the address book. Our approach is more immediate — give the bot the info upfront.

## Implementation

### New: `src/imessagarr/contacts.py`

```python
@dataclass
class ContactInfo:
    phone: str              # normalized E.164
    name: str               # full name
    nickname: str | None = None
    birthday: str | None = None   # formatted, e.g. "March 5"
    notes: str | None = None

    def to_prompt_block(self) -> str:
        """Format as <contact> block for LLM prompt."""
        lines = [f"Name: {self.name}"]
        if self.nickname:
            lines.append(f"Nickname: {self.nickname}")
        if self.birthday:
            lines.append(f"Birthday: {self.birthday}")
        if self.notes:
            lines.append(f"Notes: {self.notes}")
        return "<contact>\n" + "\n".join(lines) + "\n</contact>"


class ContactResolver:
    def __init__(self) -> None:
        self._contacts: dict[str, ContactInfo] = {}  # normalized phone → contact
        self._last_load: float = 0
        self._ttl: float = 300  # 5 minutes
        self._available: bool = False  # False if Contacts permission denied

    async def load_all(self) -> None:
        """Bulk load all contacts via one AppleScript call. Run on startup + periodically."""

    def lookup(self, phone: str) -> ContactInfo | None:
        """Instant dict lookup by normalized phone. Returns None if not a contact."""

    def is_known(self, phone: str) -> bool:
        """Check if phone is a known contact."""

    async def refresh_if_stale(self) -> None:
        """Reload if cache is older than TTL."""

    @staticmethod
    def normalize_phone(phone: str) -> str:
        """Normalize to E.164. Strip non-digits, prepend +1 if needed."""
```

### Bulk Load AppleScript

One script that returns all contacts with phone numbers:

```applescript
tell application "Contacts"
    set output to ""
    repeat with p in every person
        set pPhones to value of phones of p
        if length of pPhones > 0 then
            set pName to name of p
            set pNick to nickname of p
            set pBday to birth date of p
            set pNotes to note of p
            -- Tab-separated: name, nickname, birthday, notes, phone1, phone2, ...
            set output to output & pName & "\t" & pNick & "\t" & pBday & "\t" & pNotes
            repeat with ph in pPhones
                set output to output & "\t" & ph
            end repeat
            set output to output & "\n"
        end if
    end repeat
    return output
end tell
```

Parse the output, normalize every phone number, build the dict. Each contact with N phone numbers gets N entries in the dict (all pointing to the same `ContactInfo`). This way any of a contact's numbers will match.

### Phone Normalization (both sides, at load time)

Normalize to E.164 (`+15551234567`):
- Strip all non-digit characters except leading `+`
- If 10 digits (no country code), prepend `+1`
- If 11 digits starting with 1, prepend `+`

Applied to contact phones when building the dict AND to the incoming chat.db phone when looking up. Both sides normalized = simple dict lookup, no multi-format fallback needed.

### Modify: `src/imessagarr/monitor.py`

The inbound filter currently checks `self.allowed_senders` (a set of phones from `.env`). Change to:

```python
# In get_new_messages(), replace the allowed_senders check:
if self.contacts and self.contacts.is_known(sender):
    pass  # allowed via contacts
elif sender not in self.allowed_senders:
    log.debug("Ignoring message from unknown sender: %s", sender)
    continue
```

Accept `ContactResolver` in constructor. Call `refresh_if_stale()` at the start of each poll cycle.

### Modify: `src/imessagarr/actions.py`

- Accept `ContactResolver` in `ActionExecutor.__init__()`
- In `_build_prompt()`, after time context, inject `contact.to_prompt_block()` if available
- Use display name in log messages

### Modify: `src/imessagarr/__main__.py`

- Initialize `ContactResolver`, call `load_all()` at startup
- Add `_check_contacts_permission()` (same pattern as `_check_accessibility()`)
- Pass resolver to `MessageMonitor` and `ActionExecutor`
- Log display names: "IN Avery: ..." instead of "IN +15551234567: ..."

### Modify: `src/imessagarr/config.py`

- Make `allowed_senders` validation a warning instead of a hard error (empty is OK when contacts works)

### Modify: `src/imessagarr/types.py`

- Add `sender_name: str | None = None` to `IncomingMessage`

## Permission Model

- iMessagarr.app needs **Contacts** permission in System Settings → Privacy & Security → Contacts
- First AppleScript call to Contacts.app triggers the permission dialog
- `_check_contacts_permission()` at startup: run a simple query, log result
- If denied: `_available = False`, fall back to `ALLOWED_SENDERS` only, log warning. Bot works normally, just no names or enrichment

## Edge Cases

- **Contacts permission denied**: Graceful fallback to `ALLOWED_SENDERS`. Bot works, just no names
- **Contact has no name**: Fall back to phone number display
- **Contact card changes**: Cache refreshes every 5 min. No restart needed
- **Unknown number**: Not in contacts AND not in `ALLOWED_SENDERS` → ignored
- **Large contact list**: AppleScript bulk dump may take a few seconds for >1000 contacts. Only happens on startup + every 5 min, not on the message path
- **Contact has multiple phones**: All numbers point to the same `ContactInfo` in the dict
- **AppleScript returns empty fields**: `"missing value"` string for unset fields. Parse and treat as None
- **Contacts.app not running**: AppleScript auto-launches it in background. No issue

## Implementation Order

### Phase 1: Contact Resolution + Inbound Filter
1. `contacts.py` — `ContactInfo`, `ContactResolver` with bulk load, normalization, dict lookup
2. `types.py` — add `sender_name` to `IncomingMessage`
3. `monitor.py` — accept `ContactResolver`, use for inbound filter
4. `__main__.py` — init resolver, permission check, pass to monitor
5. `config.py` — make `allowed_senders` optional (warning, not error)

### Phase 2: Prompt Enrichment
6. `actions.py` — inject `<contact>` block into `_build_prompt()`, display names in logs
7. Test: verify the LLM uses the contact's name naturally

## Verification

1. **Permission test**: Run bot, verify Contacts permission prompt appears
2. **Known contact test**: Message from a contact → bot responds, logs show name
3. **Unknown number test**: Number not in Contacts or `ALLOWED_SENDERS` → ignored
4. **Fallback test**: Contacts permission denied → falls back to `ALLOWED_SENDERS`
5. **Prompt test**: Log full prompt, verify `<contact>` block with correct info
6. **Personalization test**: Bot uses sender's name naturally in responses
7. **Cache refresh test**: Add a contact → within 5 min, bot recognizes them
