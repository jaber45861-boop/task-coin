"""
Task Proof Image Storage (MT-ADMIN-34)
======================================

Server-side storage for manual TASK proof screenshots — the evidence
behind a ``task_submissions`` row.  It is a sibling of
``deposit_proof_storage`` (MT-ADMIN-31), NOT a reuse of it: the two
evidence families are deliberately kept apart so files, permissions
and retention never mix between deposits and tasks.

Why a separate module instead of a flag on the deposit one:

- the deposit storage owns its own env-configurable root
  (``DEPOSIT_PROOF_STORAGE_DIR``) and adding a switch would put the
  deposit path at risk of regression for a feature it does not use;
- a task screenshot is a DIFFERENT retention/audit domain, and its
  location must be predictable for an operator.

Design rules (identical guarantees, own root):

- image BYTES live on disk, never in SQLite — the row stores only a
  bounded reference, never the pixels;
- the stored reference is a *server-generated* key; a client filename
  is never stored, never used for the on-disk name, and never
  trusted;
- content validation is by MAGIC BYTES via the proven
  ``deposit_proof_storage.probe_image`` (PNG/JPEG/GIF), with the same
  byte and pixel bounds — so a decompression bomb cannot reach the
  reviewer queue;
- ``read_image`` re-validates the key shape before joining, so a
  corrupted or malicious database value can never traverse outside
  the storage root (no absolute paths, no ``..``);
- the storage root comes from ``TASK_PROOF_STORAGE_DIR`` with a
  repository-local default, resolved fresh on every call;
- nothing here ever logs image contents, and this module never
  touches the database, the wallet or the ledger.

Stable machine codes for the HTTP layer (mirroring the deposit
module's): ``unsupported_image_type``, ``file_too_large``,
``invalid_upload``.
"""

from __future__ import annotations

import os
import re
import uuid
from pathlib import Path

from deposit_proof_storage import (
    MAX_IMAGE_BYTES,
    ProofImageError,
    probe_image,
)

# ── Environment-configurable storage root (separate from deposits) ──

STORAGE_DIR_ENV = "TASK_PROOF_STORAGE_DIR"
DEFAULT_STORAGE_DIR = "task_proof_uploads"

# The only storage extension the key may carry — the extension ALWAYS
# comes from the server's own content sniffing (see probe_image).
SUPPORTED_EXTENSIONS = ("png", "jpg", "gif")


class TaskProofStorageError(Exception):
    """Base class for task-proof storage failures."""

    code = "invalid_upload"


class InvalidStorageKeyError(TaskProofStorageError):
    """A storage key failed strict shape validation."""


def storage_root() -> Path:
    """Resolved storage root (fresh on every call — env-driven)."""
    raw = os.environ.get(STORAGE_DIR_ENV, "").strip() or DEFAULT_STORAGE_DIR
    return Path(raw).expanduser().resolve()


# Server-generated key: 32 hex chars + one sniffed extension.
# Only ever produced by save_image; read_image enforces the same
# shape so a DB value can never steer the path.
_STORAGE_KEY_RE = re.compile(r"^[0-9a-f]{32}\.(?:png|jpg|gif)$")


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


def probe(data: bytes) -> tuple[str, str, int, int]:
    """Sniff one payload → ``(mime, extension, width, height)``.

    Reuses the deposit module's proven magic-byte sniffer (PNG/JPEG/
    GIF, bounded dimensions).  Extension comes from the CONTENT, the
    client-supplied filename and MIME type are never consulted.
    """
    return probe_image(data)


def save_image(
    data: bytes, *, proof_id: str, ext: str
) -> str:
    """Persist one validated image under a server-generated key.

    The caller must have validated ``data`` via :func:`probe`.
    ``proof_id`` is the server's own uuid hex — the client filename
    never reaches this function.  Returns the relative storage key
    (the value stored on the submission row).
    """
    if ext not in SUPPORTED_EXTENSIONS:
        raise TaskProofStorageError("unsupported storage extension")
    if not isinstance(proof_id, str) or not re.fullmatch(
        r"[0-9a-f]{32}", proof_id
    ):
        raise InvalidStorageKeyError("invalid proof id")
    filename = f"{proof_id}.{ext}"
    path = _validated_path(filename)
    root = storage_root()
    root.mkdir(parents=True, exist_ok=True)
    # exclusive — never overwrites an existing key
    with open(path, "xb") as handle:
        handle.write(bytes(data))
    return filename


def read_image(storage_key: object) -> bytes:
    """Read one stored evidence image back (for reviewer delivery).

    Raises:
        InvalidStorageKeyError: the key fails strict validation.
        FileNotFoundError: the evidence file is missing on disk.
    """
    path = _validated_path(storage_key)
    if not path.is_file():
        raise FileNotFoundError(
            f"evidence file missing: {storage_key}"
        )
    return path.read_bytes()


def delete_image(storage_key: object) -> None:
    """Best-effort cleanup of a just-written file (upload rollback).

    Missing files are ignored; an invalid key still raises so a bug
    can never delete outside the storage root.
    """
    path = _validated_path(storage_key)
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def is_image_reference(value: object) -> bool:
    """True when *value* is a stored task-proof image key.

    Used by the notification path to decide whether the evidence must
    be delivered as a photo.  A plain text/URL proof never matches —
    it stays text, exactly as before.
    """
    return isinstance(value, str) and bool(
        _STORAGE_KEY_RE.fullmatch(value.strip())
    )
