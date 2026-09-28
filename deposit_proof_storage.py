"""
Deposit Proof Image Storage (MT-ADMIN-31)
==========================================

Server-side storage for manual deposit-proof screenshots — the
EVIDENCE file behind a ``deposit_proofs`` row.  This module owns the
bytes on disk; it never touches the database, never touches the
wallet/ledger and knows nothing about reviews.

Design rules (smallest safe model):

- Image BYTES live on disk, never in SQLite — the database stores
  only the metadata plus the server-generated relative storage key
  (same separation the repository already uses: proof metadata in
  columns, no blobs).
- Client filenames are NEVER stored, never used for the on-disk
  name and never trusted.  The key is ``<uuid4hex>.<ext>`` where the
  extension comes from the server's own content sniffing.
- Content validation is by MAGIC BYTES, not by the client-provided
  MIME type or filename: only PNG, JPEG and GIF payment screenshots
  are accepted (arbitrary executable/script content never matches).
- Bounds: empty payloads rejected, byte size bounded
  (``MAX_IMAGE_BYTES``) and pixel dimensions bounded
  (``MAX_IMAGE_DIMENSION``) so a decompression bomb cannot reach the
  admin review queue.
- ``read_image`` re-validates the storage key against a strict shape
  before joining, so a corrupted/malicious database value can never
  traverse outside the storage root (no absolute paths, no ``..``).
- The storage root comes from ``DEPOSIT_PROOF_STORAGE_DIR`` (same
  environment-configuration convention as ``TASKCOIN_DB_PATH``) with
  a repository-local default; it is resolved fresh on every call so
  tests and operators can redirect it without touching code.
- Nothing here ever logs image contents.

Exceptions carry stable machine codes for the HTTP layer:
``unsupported_image_type``, ``file_too_large``, ``invalid_upload``.
"""

from __future__ import annotations

import os
import re
import struct
from pathlib import Path

# ── Bounds ───────────────────────────────────────────────────────────

# Hard byte ceiling for one screenshot upload (5 MiB).
MAX_IMAGE_BYTES = 5 * 1024 * 1024

# Hard pixel-dimension ceiling (width and height each).
MAX_IMAGE_DIMENSION = 10_000

# The only accepted content types — sniffed, never client-claimed.
PNG_MIME = "image/png"
JPEG_MIME = "image/jpeg"
GIF_MIME = "image/gif"
SUPPORTED_IMAGE_MIMES = (PNG_MIME, JPEG_MIME, GIF_MIME)

# Server-generated key: 32 hex chars + one of the sniffed extensions.
_STORAGE_KEY_RE = re.compile(r"[0-9a-f]{32}\.(png|jpg|gif)\Z")

# ── Environment-configurable storage root (no secrets, no code edits) ─

STORAGE_DIR_ENV = "DEPOSIT_PROOF_STORAGE_DIR"
DEFAULT_STORAGE_DIR = "deposit_proof_uploads"


def storage_root() -> Path:
    """Resolved storage root (fresh on every call — env-driven)."""
    raw = os.environ.get(STORAGE_DIR_ENV, "").strip() or DEFAULT_STORAGE_DIR
    return Path(raw).expanduser().resolve()


# ── Errors (stable machine codes) ────────────────────────────────────


class ProofImageError(Exception):
    """Base class for proof-image validation failures."""

    code = "invalid_upload"


class UnsupportedImageTypeError(ProofImageError):
    """The payload is not one of the supported image formats."""

    code = "unsupported_image_type"


class ImageTooLargeError(ProofImageError):
    """The image exceeds the byte or pixel-dimension bound."""

    code = "file_too_large"


class MalformedImageError(ProofImageError):
    """The payload claims a supported format but is structurally
    unusable (truncated/corrupted header)."""

    code = "invalid_upload"


class InvalidStorageKeyError(Exception):
    """A storage key failed strict shape validation."""


# ── Content sniffing (magic bytes + bounded dimensions) ──────────────

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")
_JPEG_MAGIC = b"\xff\xd8\xff"

# Start-of-frame markers that carry the dimensions (SOF4 = baseline
# DHT, SOF2 = progressive, …).  DHT(C4)/JPG(C8)/DAC(CC) are NOT SOF.
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
     0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _check_dimensions(width: int, height: int) -> None:
    if not (1 <= width <= MAX_IMAGE_DIMENSION and
            1 <= height <= MAX_IMAGE_DIMENSION):
        raise ImageTooLargeError(
            "image dimensions exceed the allowed bound"
        )


def _png_dimensions(data: bytes) -> tuple[int, int]:
    if len(data) < 24 or data[12:16] != b"IHDR":
        raise MalformedImageError("PNG header is malformed")
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Walk JPEG segments until the first SOF frame."""
    length = len(data)
    index = 2
    while index + 3 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        if marker == 0xDA:
            break  # scan started before any SOF — malformed
        segment_length = int.from_bytes(
            data[index + 2:index + 4], "big"
        )
        if segment_length < 2:
            raise MalformedImageError("JPEG segment length is invalid")
        if marker in _JPEG_SOF_MARKERS:
            if index + 9 > length:
                raise MalformedImageError("JPEG frame header truncated")
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return width, height
        index += 2 + segment_length
    raise MalformedImageError("JPEG frame header not found")


def probe_image(data: bytes) -> tuple[str, str, int, int]:
    """Sniff one payload → ``(mime, extension, width, height)``.

    Raises:
        MalformedImageError: empty or structurally broken payload.
        UnsupportedImageTypeError: magic bytes match no supported
            image format (executables, HTML, SVG, archives, …).
        ImageTooLargeError: dimensions beyond the bound.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise MalformedImageError("image payload must be bytes")
    data = bytes(data)
    if not data:
        raise MalformedImageError("image payload is empty")

    if data[:8] == _PNG_MAGIC:
        width, height = _png_dimensions(data)
        mime, ext = PNG_MIME, "png"
    elif data[:6] in _GIF_MAGICS:
        if len(data) < 10:
            raise MalformedImageError("GIF header is truncated")
        width, height = struct.unpack("<HH", data[6:10])
        mime, ext = GIF_MIME, "gif"
    elif data[:3] == _JPEG_MAGIC:
        width, height = _jpeg_dimensions(data)
        mime, ext = JPEG_MIME, "jpg"
    else:
        raise UnsupportedImageTypeError(
            "payload is not a PNG, JPEG or GIF image"
        )

    _check_dimensions(width, height)
    return mime, ext, width, height


# ── Write / read / delete (server-generated keys only) ───────────────


def _validated_path(storage_key: object) -> Path:
    """Strict key validation → absolute path inside the root."""
    if not isinstance(storage_key, str) or not _STORAGE_KEY_RE.fullmatch(
        storage_key
    ):
        raise InvalidStorageKeyError("invalid storage key shape")
    root = storage_root()
    path = (root / storage_key).resolve()
    if path.parent != root:
        raise InvalidStorageKeyError("storage key escapes the root")
    return path


def save_image(data: bytes, *, proof_id: str, ext: str) -> str:
    """Persist one validated image under a server-generated key.

    The caller must have validated ``data`` via :func:`probe_image`
    (and derived ``ext`` from it).  ``proof_id`` is the server's own
    uuid hex — the client filename never reaches this function.
    Returns the relative storage key for the proof row.
    """
    if ext not in ("png", "jpg", "gif"):
        raise InvalidStorageKeyError("unsupported storage extension")
    if not isinstance(proof_id, str) or not re.fullmatch(
        r"[0-9a-f]{32}", proof_id
    ):
        raise InvalidStorageKeyError("invalid proof id")
    filename = f"{proof_id}.{ext}"
    path = _validated_path(filename)
    root = storage_root()
    root.mkdir(parents=True, exist_ok=True)
    with open(path, "xb") as handle:  # exclusive — never overwrites
        handle.write(bytes(data))
    return filename


def read_image(storage_key: object) -> bytes:
    """Read one stored image back (for admin review rendering).

    Raises:
        InvalidStorageKeyError: the key fails strict validation.
        FileNotFoundError: the evidence file is missing on disk.
    """
    path = _validated_path(storage_key)
    if not path.is_file():
        raise FileNotFoundError(f"evidence file missing: {storage_key}")
    return path.read_bytes()


def delete_image(storage_key: object) -> None:
    """Best-effort cleanup of a just-written file (upload rollback).

    Missing files are ignored; invalid keys still raise so a bug can
    never delete outside the storage root.
    """
    path = _validated_path(storage_key)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
