"""Tests for parse_attributed_body — extracting text from chat.db attributedBody blobs.

Outgoing iMessages sent via AppleScript have text=NULL in chat.db and store
content only in attributedBody (a typedstream-encoded NSAttributedString).
The parser must handle:
- Short strings (length < 128): single-byte length encoding
- Medium strings (128-255): 0x81 + 2-byte little-endian length
- Long strings (256+): 0x81 + 2-byte little-endian length
- Image attachments: should return None
"""

import pytest

from bluepopcorn.monitor import _read_typedstream_length, parse_attributed_body

# --- Typedstream blob builder ---
# Real format from macOS Sequoia chat.db:
#   [streamtyped header] NSAttributedString [class info]
#   NSObject [class info] NSString [01 94/95 84 01 2b] [length] [text] [attrs]

# Common prefix: streamtyped header + NSAttributedString + NSObject + NSString + header
_BLOB_PREFIX = bytes.fromhex(
    "040b73747265616d747970656481e803840140"  # streamtyped header
    "848484124e5341747472696275746564537472696e67"  # NSAttributedString
    "008484084e534f626a65637400"  # NSObject
    "8592848484084e53537472696e67"  # NSString
    "019484012b"  # header before '+' length marker
)

# Suffix: minimal trailing attribute data (enough to not truncate)
_BLOB_SUFFIX = bytes.fromhex("0684" + "00" * 20)


def _make_blob(text: str) -> bytes:
    """Build a realistic attributedBody blob for a text message."""
    text_bytes = text.encode("utf-8")
    byte_len = len(text_bytes)
    if byte_len < 0x80:
        length_encoding = bytes([byte_len])
    else:
        # 0x81 + 2-byte little-endian
        length_encoding = b"\x81" + byte_len.to_bytes(2, "little")
    return _BLOB_PREFIX + length_encoding + text_bytes + _BLOB_SUFFIX


def _make_image_blob() -> bytes:
    """Build a realistic attributedBody blob for an image attachment."""
    # Image messages have the object replacement char (U+FFFC) as the "text",
    # followed by NSDictionary attachment metadata
    return bytes.fromhex(
        "040b73747265616d747970656481e803840140"
        "848484124e5341747472696275746564537472696e67"
        "008484084e534f626a65637400"
        "8592848484084e53537472696e67"
        "019484012b"
        "03"  # length = 3
        "efbfbc"  # U+FFFC object replacement character (UTF-8)
        "86840269490101"  # attachment metadata starts
        "928484840c4e5344696374696f6e617279"  # NSDictionary
        "009484016902"
    )


# --- Tests for _read_typedstream_length ---


class TestReadTypedstreamLength:
    def test_single_byte(self):
        data = bytes([67])  # 0x43
        assert _read_typedstream_length(data, 0) == (67, 1)

    def test_single_byte_max(self):
        data = bytes([127])  # 0x7f — largest single-byte value
        assert _read_typedstream_length(data, 0) == (127, 1)

    def test_two_byte_little_endian(self):
        # 0x81 0x84 0x00 → 0x0084 = 132
        data = bytes([0x81, 0x84, 0x00])
        assert _read_typedstream_length(data, 0) == (132, 3)

    def test_two_byte_little_endian_large(self):
        # 0x81 0x6a 0x01 → 0x016a = 362
        data = bytes([0x81, 0x6A, 0x01])
        assert _read_typedstream_length(data, 0) == (362, 3)

    def test_two_byte_at_offset(self):
        data = bytes([0xFF, 0xFF, 0x81, 0x84, 0x00])
        assert _read_typedstream_length(data, 2) == (132, 5)

    def test_four_byte_little_endian(self):
        # 0x82 + 4 bytes little-endian
        data = bytes([0x82, 0x00, 0x04, 0x00, 0x00])  # 1024
        assert _read_typedstream_length(data, 0) == (1024, 5)

    def test_empty_data(self):
        assert _read_typedstream_length(b"", 0) == (0, 0)

    def test_offset_past_end(self):
        assert _read_typedstream_length(b"\x42", 5) == (0, 5)

    def test_truncated_two_byte(self):
        # 0x81 but only 1 byte follows instead of 2
        data = bytes([0x81, 0x84])
        val, off = _read_typedstream_length(data, 0)
        assert val == 0  # Should fail gracefully


# --- Tests for parse_attributed_body ---


class TestParseAttributedBody:
    def test_short_message(self):
        """Messages < 128 chars use single-byte length."""
        text = "Requested Peaky Blinders: The Immortal Man (2026)."
        assert len(text) < 128
        blob = _make_blob(text)
        assert parse_attributed_body(blob) == text

    def test_medium_message_132_chars(self):
        """Messages 128-255 chars use 0x81 + 2-byte LE length."""
        text = (
            "Which one? Peaky Blinders: The Immortal Man (2026), "
            "Peaky Blinders: The True Story (2016), "
            "or Peaky Blinders: The Real Story (2026)?"
        )
        assert 128 <= len(text.encode("utf-8")) < 256
        blob = _make_blob(text)
        assert parse_attributed_body(blob) == text

    def test_long_message_362_bytes(self):
        """Messages > 255 bytes use 0x81 + 2-byte LE length."""
        text = (
            "There are three: Peaky Blinders: The Immortal Man (2026) is the "
            "main one \u2014 it's about a self-exiled gangster named Tomm whose "
            "estranged son gets mixed up in a Nazi plot. It's rated 8.6/10. "
            "The other two are Peaky Blinders: The Real Story (2026) and "
            "Peaky Blinders: The True Story (2016). Want to add any of them?"
        )
        assert len(text.encode("utf-8")) > 256
        blob = _make_blob(text)
        assert parse_attributed_body(blob) == text

    def test_message_with_emoji(self):
        text = "Added to your list! \U0001f37f"
        blob = _make_blob(text)
        assert parse_attributed_body(blob) == text

    def test_message_with_em_dash(self):
        """Em dash is 3 bytes in UTF-8 — byte length != char length."""
        text = "Severance (2022) \u2014 rated 8.4/10. A thriller about office workers."
        blob = _make_blob(text)
        result = parse_attributed_body(blob)
        assert result == text

    def test_image_attachment_returns_none(self):
        """Image-only messages (object replacement char) should return None."""
        blob = _make_image_blob()
        assert parse_attributed_body(blob) is None

    def test_none_blob(self):
        assert parse_attributed_body(None) is None

    def test_empty_blob(self):
        assert parse_attributed_body(b"") is None

    def test_garbage_blob(self):
        assert parse_attributed_body(b"\x00\x01\x02random garbage") is None

    def test_no_nsstring_marker(self):
        blob = b"streamtyped stuff but no string marker here"
        assert parse_attributed_body(blob) is None

    def test_real_hex_short(self):
        """Real hex dump from ROWID=333 (67-char message)."""
        # First 120 bytes from actual chat.db diagnostic
        hex_prefix = (
            "040b73747265616d747970656481e803840140"
            "848484124e5341747472696275746564537472696e67"
            "008484084e534f626a65637400"
            "8592848484084e53537472696e67"
            "019484012b"
            "43"  # length = 67
        )
        text = "Requested Peaky Blinders: The Immortal Man (2026) \u2014 rated 8.6/10."
        text_bytes = text.encode("utf-8")
        blob = bytes.fromhex(hex_prefix) + text_bytes + b"\x06\x84" + b"\x00" * 50
        result = parse_attributed_body(blob)
        assert result is not None
        assert result.startswith("Requested Peaky Blinders")

    def test_real_hex_medium(self):
        """Real post-NSString bytes from ROWID=331 (132-char message)."""
        # Construct blob with real header + 0x81 length encoding
        text = (
            "Which one? Peaky Blinders: The Immortal Man (2026), "
            "Peaky Blinders: The True Story (2016), "
            "or Peaky Blinders: The Real Story (2026)?"
        )
        blob = _make_blob(text)
        result = parse_attributed_body(blob)
        assert result == text

    def test_real_hex_long(self):
        """Real post-NSString bytes from ROWID=327 (362-byte message)."""
        text = (
            "There are three: Peaky Blinders: The Immortal Man (2026) is the "
            "main one \u2014 it's about a self-exiled gangster named Tomm whose "
            "estranged son gets mixed up in a Nazi plot. It's rated 8.6/10. "
            "The other two are Peaky Blinders: The Real Story (2026) and "
            "Peaky Blinders: The True Story (2016). Want to add any of them?"
        )
        blob = _make_blob(text)
        result = parse_attributed_body(blob)
        assert result == text
        assert len(result.encode("utf-8")) > 256
