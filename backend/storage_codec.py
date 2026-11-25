"""Lossless, versioned storage for large JSON strings; callers still receive text."""
import base64
import zlib
from sqlalchemy import Text
from sqlalchemy.types import TypeDecorator

PREFIX = 'ntz1:'
MIN_BYTES = 512
MAX_DECOMPRESSED_BYTES = 128 * 1024 * 1024


def unpack_text(value):
    if value is None or not value.startswith(PREFIX):
        return value
    try:
        packed = base64.b64decode(value[len(PREFIX):], validate=True)
        decoder = zlib.decompressobj()
        raw = decoder.decompress(packed, MAX_DECOMPRESSED_BYTES + 1)
        if len(raw) > MAX_DECOMPRESSED_BYTES or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
            raise ValueError('Invalid compressed payload size or framing')
        return raw.decode('utf-8')
    except (ValueError, zlib.error, UnicodeError) as exc:
        raise ValueError('Não foi possível ler um campo compactado do banco.') from exc


def pack_text(value):
    if value is None:
        return None
    if value.startswith(PREFIX):
        unpack_text(value)  # Validate imported data and avoid double encoding.
        return value
    raw = value.encode('utf-8')
    if len(raw) < MIN_BYTES or len(raw) > MAX_DECOMPRESSED_BYTES:
        return value
    encoded = PREFIX + base64.b64encode(zlib.compress(raw, level=6)).decode('ascii')
    return encoded if len(encoded) <= len(raw) - 64 else value


class PackedJSONText(TypeDecorator):
    """TEXT on disk, compatible with both legacy plaintext and packed records."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return pack_text(value)

    def process_result_value(self, value, dialect):
        return unpack_text(value)
