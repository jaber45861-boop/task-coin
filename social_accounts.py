"""
Social Account Service (SA-YT-01)
=================================

Provider-independent persistence for linked social accounts, plus the
encrypted token store that OAuth providers require.

Design rules:
- Provider-agnostic: YouTube is the first provider, but nothing here
  imports or knows about Google/YouTube — callers pass ``provider``.
- The stable provider identity (``provider_user_id``, e.g. the YouTube
  channel id) is the link identity.  A display name is never identity.
- OAuth tokens are secrets: they are encrypted at rest with a Fernet
  key from the environment, never returned by :class:`SocialAccount`,
  never logged, and never exposed through any API response.
- Missing/invalid encryption configuration fails closed — tokens are
  refused rather than written in plaintext.
- Uniqueness is enforced by the database, inside the existing
  ``db.transaction()`` BEGIN IMMEDIATE boundary:
    * ``(provider, provider_user_id)`` — a channel belongs to one user
    * ``(user_id, provider) WHERE status='linked'`` — no duplicate
      active links for the same Telegram user

No wallet/ledger/task/Telegram business logic lives here.
"""

import logging
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import db

logger = logging.getLogger(__name__)

# ── Provider / status constants ────────────────────────────────────────
PROVIDER_YOUTUBE = "youtube"
SUPPORTED_PROVIDERS: frozenset[str] = frozenset({PROVIDER_YOUTUBE})

STATUS_LINKED = "linked"
STATUS_REVOKED = "revoked"

# Environment variable holding the Fernet key used to encrypt OAuth
# tokens at rest.  Never hard-coded; the application fails closed when
# it is missing or malformed.
TOKEN_ENCRYPTION_KEY_ENV = "SOCIAL_TOKEN_ENCRYPTION_KEY"

_FERNET_KEY_HINT = (
    'generate one with: python -c "from cryptography.fernet import '
    'Fernet; print(Fernet.generate_key().decode())"'
)


# ── Errors ─────────────────────────────────────────────────────────────
class SocialAccountError(Exception):
    """Base error for social account persistence."""


class SocialAccountConfigError(SocialAccountError):
    """Encryption-at-rest configuration is missing or invalid (fail closed)."""


class DuplicateLinkError(SocialAccountError):
    """The provider account already belongs to a different user."""


# ── Encryption at rest ─────────────────────────────────────────────────
try:  # pragma: no cover - exercised via the availability tests
    from cryptography.fernet import Fernet, InvalidToken

    _CRYPTOGRAPHY_AVAILABLE = True
except ImportError:  # pragma: no cover
    Fernet = None  # type: ignore[assignment]
    InvalidToken = None  # type: ignore[assignment]
    _CRYPTOGRAPHY_AVAILABLE = False


class TokenCipher:
    """Authenticated encryption-at-rest for OAuth tokens.

    Uses Fernet (AES-128-CBC + HMAC-SHA256) from the standard
    ``cryptography`` package — a standard authenticated-encryption
    primitive, never a home-made construction.
    """

    def __init__(self, key: bytes) -> None:
        if not _CRYPTOGRAPHY_AVAILABLE:
            raise SocialAccountConfigError(
                "the 'cryptography' package is required to encrypt OAuth "
                "tokens (see requirements.txt)"
            )
        try:
            self._fernet = Fernet(key)
        except (TypeError, ValueError) as exc:
            raise SocialAccountConfigError(
                f"{TOKEN_ENCRYPTION_KEY_ENV} is not a valid Fernet key — "
                f"{_FERNET_KEY_HINT}"
            ) from exc

    @classmethod
    def from_environment(cls) -> "TokenCipher":
        """Build the cipher from the environment; fail closed if absent."""
        raw = os.environ.get(TOKEN_ENCRYPTION_KEY_ENV, "").strip()
        if not raw:
            raise SocialAccountConfigError(
                f"{TOKEN_ENCRYPTION_KEY_ENV} environment variable is not "
                "set — refusing to store OAuth tokens unencrypted "
                f"({_FERNET_KEY_HINT})"
            )
        return cls(raw.encode("utf-8"))

    def encrypt(self, plaintext: str) -> str:
        """Encrypt a token to a urlsafe-base64 ciphertext string."""
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, ciphertext: str) -> str:
        """Decrypt a stored ciphertext; raises on integrity failure."""
        try:
            return self._fernet.decrypt(ciphertext.encode("ascii")).decode(
                "utf-8"
            )
        except InvalidToken:
            raise SocialAccountError(
                "stored token failed integrity verification"
            ) from None


# ── Value objects ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class SocialAccount:
    """A linked social account as safe application/UI data.

    Deliberately carries no token material of any kind.
    """

    id: int
    user_id: int
    provider: str
    provider_user_id: str
    username: str | None
    display_name: str | None
    status: str
    scopes: str
    created_at: str | None
    updated_at: str | None
    last_verified_at: str | None

    @property
    def is_linked(self) -> bool:
        return self.status == STATUS_LINKED


@dataclass(frozen=True)
class OAuthTokens:
    """Decrypted token material — for backend use only, never a response."""

    access_token: str
    refresh_token: str | None
    expires_at: str | None
    scopes: str


def _utc_now() -> str:
    """UTC timestamp in the same format SQLite CURRENT_TIMESTAMP uses."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _expires_at(expires_in: int | None) -> str | None:
    """Absolute UTC expiry (SQLite timestamp format) from a lifetime."""
    if not expires_in:
        return None
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    moment = datetime.now(timezone.utc) + timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def _row_to_account(row: sqlite3.Row) -> SocialAccount:
    """Map a row to :class:`SocialAccount` — token columns never included."""
    return SocialAccount(
        id=row["id"],
        user_id=row["user_id"],
        provider=row["provider"],
        provider_user_id=row["provider_user_id"],
        username=row["username"],
        display_name=row["display_name"],
        status=row["status"],
        scopes=row["scopes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_verified_at=row["last_verified_at"],
    )


# ── Service ────────────────────────────────────────────────────────────
class SocialAccountService:
    """Generic linked-account operations (provider-independent)."""

    @staticmethod
    def link(
        user_id: int,
        provider: str,
        provider_user_id: str,
        scopes: str,
        access_token: str | None = None,
        refresh_token: str | None = None,
        expires_in: int | None = None,
        display_name: str | None = None,
        username: str | None = None,
        db_path: str | None = None,
    ) -> SocialAccount:
        """Create or refresh a link between a user and a provider account.

        Runs inside the repository's ``BEGIN IMMEDIATE`` transaction so
        uniqueness is decided atomically:

        - a provider account already owned by another user is rejected
        - re-linking replaces the user's previous active link for the
          same provider (never two active rows for one pair)

        Raises:
            SocialAccountConfigError: encryption key missing/invalid —
                fails closed before anything is written.
            DuplicateLinkError: provider account owned by another user.
            SocialAccountError: invalid input or storage failure.
        """
        if provider not in SUPPORTED_PROVIDERS:
            raise SocialAccountError(f"unsupported provider: {provider!r}")
        if not user_id:
            raise SocialAccountError("user_id is required")
        if not provider_user_id or not isinstance(provider_user_id, str):
            raise SocialAccountError("provider_user_id is required")
        if not access_token:
            raise SocialAccountError("access_token is required")

        # Fail closed before touching the database.
        cipher = TokenCipher.from_environment()
        access_enc = cipher.encrypt(access_token)
        refresh_enc = cipher.encrypt(refresh_token) if refresh_token else None
        now = _utc_now()
        expires_at = _expires_at(expires_in)

        try:
            with db.transaction(db_path) as conn:
                existing = conn.execute(
                    "SELECT user_id FROM social_accounts "
                    "WHERE provider = ? AND provider_user_id = ?",
                    (provider, provider_user_id),
                ).fetchone()
                if existing is not None and existing["user_id"] != user_id:
                    raise DuplicateLinkError(
                        "this provider account is already linked "
                        "to another user"
                    )

                # One active link per (user, provider): replace the old one.
                conn.execute(
                    "DELETE FROM social_accounts "
                    "WHERE user_id = ? AND provider = ? AND status = ? "
                    "AND provider_user_id <> ?",
                    (user_id, provider, STATUS_LINKED, provider_user_id),
                )

                conn.execute(
                    """
                    INSERT INTO social_accounts (
                        user_id, provider, provider_user_id, username,
                        display_name, status, access_token_encrypted,
                        refresh_token_encrypted, token_expires_at, scopes,
                        created_at, updated_at, last_verified_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(provider, provider_user_id) DO UPDATE SET
                        user_id = excluded.user_id,
                        username = excluded.username,
                        display_name = excluded.display_name,
                        status = excluded.status,
                        access_token_encrypted = excluded.access_token_encrypted,
                        refresh_token_encrypted = excluded.refresh_token_encrypted,
                        token_expires_at = excluded.token_expires_at,
                        scopes = excluded.scopes,
                        updated_at = excluded.updated_at,
                        last_verified_at = excluded.last_verified_at
                    """,
                    (
                        user_id,
                        provider,
                        provider_user_id,
                        username,
                        display_name,
                        STATUS_LINKED,
                        access_enc,
                        refresh_enc,
                        expires_at,
                        scopes,
                        now,
                        now,
                        now,
                    ),
                )

                row = conn.execute(
                    "SELECT * FROM social_accounts "
                    "WHERE provider = ? AND provider_user_id = ?",
                    (provider, provider_user_id),
                ).fetchone()
        except sqlite3.IntegrityError as exc:
            raise SocialAccountError(
                "social account insert violated a database constraint"
            ) from exc

        if row is None:  # pragma: no cover - defensive
            raise SocialAccountError("failed to read back the linked account")
        return _row_to_account(row)

    @staticmethod
    def get(
        user_id: int, provider: str, db_path: str | None = None
    ) -> SocialAccount | None:
        """Return the user's active link for ``provider``, if any."""
        with db.get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT * FROM social_accounts "
                "WHERE user_id = ? AND provider = ? AND status = ?",
                (user_id, provider, STATUS_LINKED),
            ).fetchone()
        return _row_to_account(row) if row else None

    @staticmethod
    def list_accounts(
        user_id: int, db_path: str | None = None
    ) -> list[SocialAccount]:
        """Return every active link for ``user_id`` (safe fields only)."""
        with db.get_connection(db_path) as conn:
            rows = conn.execute(
                "SELECT * FROM social_accounts "
                "WHERE user_id = ? AND status = ? ORDER BY id",
                (user_id, STATUS_LINKED),
            ).fetchall()
        return [_row_to_account(r) for r in rows]

    @staticmethod
    def load_tokens(
        user_id: int, provider: str, db_path: str | None = None
    ) -> OAuthTokens | None:
        """Decrypt stored tokens for backend use (never an API response)."""
        with db.get_connection(db_path) as conn:
            row = conn.execute(
                "SELECT access_token_encrypted, refresh_token_encrypted, "
                "token_expires_at, scopes FROM social_accounts "
                "WHERE user_id = ? AND provider = ? AND status = ?",
                (user_id, provider, STATUS_LINKED),
            ).fetchone()
        if row is None or not row["access_token_encrypted"]:
            return None
        cipher = TokenCipher.from_environment()
        return OAuthTokens(
            access_token=cipher.decrypt(row["access_token_encrypted"]),
            refresh_token=(
                cipher.decrypt(row["refresh_token_encrypted"])
                if row["refresh_token_encrypted"]
                else None
            ),
            expires_at=row["token_expires_at"],
            scopes=row["scopes"],
        )

    @staticmethod
    def unlink(
        user_id: int, provider: str, db_path: str | None = None
    ) -> bool:
        """Delete ``user_id``'s link (and its tokens) for ``provider``.

        Only ever touches the calling user's own row — one user can
        never unlink another user's account.
        """
        with db.transaction(db_path) as conn:
            cursor = conn.execute(
                "DELETE FROM social_accounts "
                "WHERE user_id = ? AND provider = ? AND status = ?",
                (user_id, provider, STATUS_LINKED),
            )
            removed = cursor.rowcount > 0
        if removed:
            logger.info(
                "Unlinked provider %s for user %s", provider, user_id
            )
        return removed
