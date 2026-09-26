"""
Generic task taxonomy (MT-ADMIN-05)
====================================

Stable, machine-readable identifiers for the generic micro-task
platform: task families, providers, per-provider actions and the
verification modes the product actually supports.

Shared by:
- ``admin_task_wizard``   — the step-by-step creation UI (labels only)
- ``task_creation``       — the canonical creation service (whitelists)
- ``manual_task``         — the manual-proof contract whitelist

Design rules (MT-ADMIN-05):
- Stored values are stable machine identifiers (``instagram``,
  ``google_play``, ``join_channel``) — never localized display text.
- Selecting a provider never implies a provider API or automatic
  verification exists.  Only Telegram membership has a real automatic
  verifier today (``telegram_channel_task_verifier``); every other
  family is manual proof / approval review through the existing
  MT-TASK-15 services.
- This module imports NOTHING from the project, so it can never form
  an import cycle.

Run:
    python3 -m pytest test_admin_task_wizard.py -v
"""

from __future__ import annotations

import re

# ── Task families (wizard step 1) ─────────────────────────────────────
# Coarse grouping for the UI only — nothing is ever persisted as a
# family; the stable provider id is what lands in task_data.

FAMILY_TELEGRAM = "telegram"
FAMILY_SOCIAL = "social"
FAMILY_WEBSITE = "website"
FAMILY_APP = "app"
FAMILY_CRYPTO = "crypto"
FAMILY_OTHER = "other"

FAMILIES = (
    FAMILY_TELEGRAM,
    FAMILY_SOCIAL,
    FAMILY_WEBSITE,
    FAMILY_APP,
    FAMILY_CRYPTO,
    FAMILY_OTHER,
)

FAMILY_LABELS = {
    FAMILY_TELEGRAM: "✈️ Telegram",
    FAMILY_SOCIAL: "📱 سوشيال ميديا",
    FAMILY_WEBSITE: "🌐 مواقع",
    FAMILY_APP: "📲 تطبيقات",
    FAMILY_CRYPTO: "🪙 كريبتو",
    FAMILY_OTHER: "📦 أخرى",
}

# ── Providers (wizard step 2) ─────────────────────────────────────────
# Generic identifiers sufficient for the current product.  Selecting a
# provider is task-creation metadata ONLY — no provider API exists in
# this task and none is implied.

PROVIDER_TELEGRAM = "telegram"
PROVIDER_INSTAGRAM = "instagram"
PROVIDER_TIKTOK = "tiktok"
PROVIDER_VK = "vk"
PROVIDER_LINKEDIN = "linkedin"
PROVIDER_REDDIT = "reddit"
PROVIDER_LIKEE = "likee"
PROVIDER_YOUTUBE = "youtube"
PROVIDER_WEBSITE = "website"
PROVIDER_GOOGLE_PLAY = "google_play"
PROVIDER_APP_STORE = "app_store"
PROVIDER_CRYPTO = "crypto"
PROVIDER_OTHER = "other"

GENERIC_PROVIDERS = (
    PROVIDER_TELEGRAM,
    PROVIDER_INSTAGRAM,
    PROVIDER_TIKTOK,
    PROVIDER_VK,
    PROVIDER_LINKEDIN,
    PROVIDER_REDDIT,
    PROVIDER_LIKEE,
    PROVIDER_YOUTUBE,
    PROVIDER_WEBSITE,
    PROVIDER_GOOGLE_PLAY,
    PROVIDER_APP_STORE,
    PROVIDER_CRYPTO,
    PROVIDER_OTHER,
)

GENERIC_PROVIDER_SET = frozenset(GENERIC_PROVIDERS)

PROVIDER_LABELS = {
    PROVIDER_TELEGRAM: "Telegram",
    PROVIDER_INSTAGRAM: "Instagram",
    PROVIDER_TIKTOK: "TikTok",
    PROVIDER_VK: "VK",
    PROVIDER_LINKEDIN: "LinkedIn",
    PROVIDER_REDDIT: "Reddit",
    PROVIDER_LIKEE: "Likee",
    PROVIDER_YOUTUBE: "YouTube",
    PROVIDER_WEBSITE: "🌐 موقع ويب",
    PROVIDER_GOOGLE_PLAY: "Google Play",
    PROVIDER_APP_STORE: "App Store",
    PROVIDER_CRYPTO: "🪙 منصة كريبتو",
    PROVIDER_OTHER: "أخرى",
}

FAMILY_PROVIDERS = {
    FAMILY_TELEGRAM: (PROVIDER_TELEGRAM,),
    FAMILY_SOCIAL: (
        PROVIDER_INSTAGRAM,
        PROVIDER_TIKTOK,
        PROVIDER_VK,
        PROVIDER_LINKEDIN,
        PROVIDER_REDDIT,
        PROVIDER_LIKEE,
        PROVIDER_YOUTUBE,
    ),
    FAMILY_WEBSITE: (PROVIDER_WEBSITE,),
    FAMILY_APP: (PROVIDER_GOOGLE_PLAY, PROVIDER_APP_STORE),
    FAMILY_CRYPTO: (PROVIDER_CRYPTO,),
    FAMILY_OTHER: (PROVIDER_OTHER,),
}

# ── Actions (wizard step 4) ───────────────────────────────────────────
# Generic identifiers only — no provider-specific columns anywhere.
# The per-provider mapping below is EXPLICIT: an action is offered for
# a provider only when it is listed here.  "referral" is deliberately
# absent — the referral action belongs to the existing MT-TASK-06
# referral family, not to wizard-created tasks.

ACTION_JOIN_CHANNEL = "join_channel"
ACTION_FOLLOW = "follow"
ACTION_LIKE = "like"
ACTION_COMMENT = "comment"
ACTION_VISIT = "visit"
ACTION_OPEN = "open"
ACTION_DOWNLOAD = "download"
ACTION_WATCH = "watch"
ACTION_START = "start"
ACTION_SUBMIT_PROOF = "submit_proof"
ACTION_PROOF = "proof"   # legacy MT-TASK-15 manual action (never offered)

_SOCIAL_ACTIONS = (
    ACTION_FOLLOW, ACTION_LIKE, ACTION_COMMENT, ACTION_VISIT,
    ACTION_SUBMIT_PROOF,
)
_WEB_ACTIONS = (ACTION_VISIT, ACTION_OPEN, ACTION_SUBMIT_PROOF)
_APP_ACTIONS = (ACTION_DOWNLOAD, ACTION_OPEN, ACTION_SUBMIT_PROOF)

ACTIONS_BY_PROVIDER = {
    PROVIDER_TELEGRAM: (
        ACTION_JOIN_CHANNEL, ACTION_START, ACTION_VISIT,
        ACTION_SUBMIT_PROOF,
    ),
    PROVIDER_INSTAGRAM: _SOCIAL_ACTIONS,
    PROVIDER_TIKTOK: _SOCIAL_ACTIONS,
    PROVIDER_VK: _SOCIAL_ACTIONS,
    PROVIDER_LINKEDIN: _SOCIAL_ACTIONS,
    PROVIDER_REDDIT: _SOCIAL_ACTIONS,
    PROVIDER_LIKEE: _SOCIAL_ACTIONS,
    PROVIDER_YOUTUBE: (
        ACTION_WATCH, ACTION_LIKE, ACTION_COMMENT, ACTION_VISIT,
        ACTION_SUBMIT_PROOF,
    ),
    PROVIDER_WEBSITE: _WEB_ACTIONS,
    PROVIDER_GOOGLE_PLAY: _APP_ACTIONS,
    PROVIDER_APP_STORE: _APP_ACTIONS,
    PROVIDER_CRYPTO: _WEB_ACTIONS,
    PROVIDER_OTHER: (
        ACTION_VISIT, ACTION_OPEN, ACTION_DOWNLOAD, ACTION_WATCH,
        ACTION_START, ACTION_FOLLOW, ACTION_LIKE, ACTION_COMMENT,
        ACTION_SUBMIT_PROOF,
    ),
}

ACTION_LABELS = {
    ACTION_JOIN_CHANNEL: "انضمام لقناة",
    ACTION_FOLLOW: "متابعة",
    ACTION_LIKE: "إعجاب",
    ACTION_COMMENT: "تعليق",
    ACTION_VISIT: "زيارة",
    ACTION_OPEN: "فتح",
    ACTION_DOWNLOAD: "تحميل",
    ACTION_WATCH: "مشاهدة",
    ACTION_START: "بدء",
    ACTION_SUBMIT_PROOF: "إرسال إثبات",
    ACTION_PROOF: "إرسال إثبات يدوي",
}

# Every action the wizard can ever place in a manual task contract,
# plus the legacy "proof" action of the MT-TASK-15 contract.  The
# referral action is intentionally NOT part of this whitelist.
MANUAL_TASK_ACTIONS = frozenset(
    {ACTION_PROOF}
    | {a for actions in ACTIONS_BY_PROVIDER.values() for a in actions}
)

# ── Verification modes (wizard step 6) ────────────────────────────────
# Capability-based, not marketing-based:
#   auto     — ONLY the existing Telegram membership verifier; the
#              resulting task is type telegram_channel with the exact
#              MT-TASK-05 contract.
#   manual   — worker submits a proof reference, the task-specific
#              approver reviews it (existing MT-TASK-15 services).
#   approval — the same existing approval-gated claim flow, with the
#              approver chosen explicitly in the wizard.
# No other automatic verifier exists and none is implied.

VERIFICATION_AUTO = "auto"
VERIFICATION_MANUAL = "manual"
VERIFICATION_APPROVAL = "approval"
VERIFICATION_MODES = (
    VERIFICATION_AUTO,
    VERIFICATION_MANUAL,
    VERIFICATION_APPROVAL,
)
VERIFICATION_MODE_SET = frozenset(VERIFICATION_MODES)

VERIFICATION_LABELS = {
    VERIFICATION_AUTO: "✅ تحقق تلقائي (عضوية Telegram)",
    VERIFICATION_MANUAL: "🔍 مراجعة يدوية (إثبات + مراجعة)",
    VERIFICATION_APPROVAL: "🔒 موافقة صريحة من محدد",
}

# ── Text bounds (shared; the contract validators re-enforce theirs) ───

MAX_TITLE_LENGTH = 200
# Mirrors telegram_channel_task_verifier.MAX_INSTRUCTIONS_LENGTH so the
# writer never produces instructions the reader rejects.
MAX_INSTRUCTIONS_LENGTH = 1000
MAX_TARGET_REF_LENGTH = 500
MAX_TARGET_LABEL_LENGTH = 120

# C0/C1 control characters minus the tab (\x09): LF/CR are handled
# explicitly by the caller, everything else (NULs, escapes, DEL, C1)
# is never safe task content.
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")

# Arabic-Indic digits (U+0660..U+0669) normalized before parsing so a
# typed "٥٠٠" is a number, while "5.5" / "-5" / "abc" stay rejected.
_ARABIC_INDIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")

_ASCII_INT_RE = re.compile(r"[0-9]+")


def has_unsafe_control_chars(text: str, *, allow_newlines: bool) -> bool:
    """True when *text* carries control characters that must never be
    persisted as task content.

    The tab is always accepted; LF (and CR) are accepted only when
    *allow_newlines* is true.  Everything in the C0/C1 set is unsafe.
    """
    if not isinstance(text, str):
        return True
    probe = text.replace("\t", "")
    if allow_newlines:
        probe = probe.replace("\n", "").replace("\r", "")
    return bool(_UNSAFE_CONTROL_RE.search(probe))


def validate_title(value: object) -> str:
    """Strip + validate a task title: non-empty, single line, bounded,
    no unsafe control characters.  Returns the cleaned title.

    Raises:
        ValueError: on any violation (Arabic message, shown to admin).
    """
    if not isinstance(value, str):
        raise ValueError("❌ العنوان يجب أن يكون نصًا.")
    title = value.strip()
    if not title:
        raise ValueError("❌ العنوان لا يمكن أن يكون فارغًا.")
    if len(title) > MAX_TITLE_LENGTH:
        raise ValueError(
            f"❌ العنوان يتجاوز {MAX_TITLE_LENGTH} حرفًا."
        )
    if "\n" in title or "\r" in title:
        raise ValueError("❌ العنوان يجب أن يكون في سطر واحد.")
    if has_unsafe_control_chars(title, allow_newlines=False):
        raise ValueError("❌ العنوان يحتوي على أحرف غير مسموحة.")
    return title


def validate_instructions(value: object) -> str:
    """Strip + validate worker instructions: non-empty, bounded, no
    unsafe control characters (newlines are allowed)."""
    if not isinstance(value, str):
        raise ValueError("❌ التعليمات يجب أن تكون نصًا.")
    instructions = value.strip()
    if not instructions:
        raise ValueError("❌ التعليمات لا يمكن أن تكون فارغة.")
    if len(instructions) > MAX_INSTRUCTIONS_LENGTH:
        raise ValueError(
            f"❌ التعليمات تتجاوز {MAX_INSTRUCTIONS_LENGTH} حرفًا."
        )
    if has_unsafe_control_chars(instructions, allow_newlines=True):
        raise ValueError("❌ التعليمات تحتوي على أحرف غير مسموحة.")
    return instructions


def validate_target_ref(value: object) -> str:
    """Strip + validate a generic target reference (URL, slug, id).

    Deliberately NOT an HTTP validator: supported providers need
    non-HTTP identifiers (store ids, crypto handles, channel slugs).
    """
    if not isinstance(value, str):
        raise ValueError("❌ الهدف يجب أن يكون نصًا.")
    ref = value.strip()
    if not ref:
        raise ValueError("❌ الهدف لا يمكن أن يكون فارغًا.")
    if len(ref) > MAX_TARGET_REF_LENGTH:
        raise ValueError(
            f"❌ الهدف يتجاوز {MAX_TARGET_REF_LENGTH} حرفًا."
        )
    if has_unsafe_control_chars(ref, allow_newlines=False):
        raise ValueError("❌ الهدف يحتوي على أحرف غير مسموحة.")
    return ref


def normalize_digits(text: str) -> str:
    """Translate Arabic-Indic digits to ASCII (identity otherwise)."""
    return text.translate(_ARABIC_INDIC_DIGITS)


def parse_non_negative_int(value: object, *, field: str) -> int:
    """Parse a bounded, non-negative integer from admin text.

    Rejects: non-numeric text, decimals, signs, empty strings and
    booleans.  Returns ``int >= 0``.

    Raises:
        ValueError: malformed input (Arabic message).
    """
    if not isinstance(value, str):
        raise ValueError(f"❌ {field} يجب أن يكون رقمًا صحيحًا.")
    text = normalize_digits(value.strip())
    if not _ASCII_INT_RE.fullmatch(text):
        raise ValueError(
            f"❌ {field} يجب أن يكون عددًا صحيحًا موجبيًا (0 أو أكثر)."
        )
    return int(text)


def parse_positive_int(value: object, *, field: str) -> int:
    """Parse an integer >= 1 (repeat_hours and similar)."""
    number = parse_non_negative_int(value, field=field)
    if number < 1:
        raise ValueError(f"❌ {field} يجب أن يكون 1 أو أكثر.")
    return number
