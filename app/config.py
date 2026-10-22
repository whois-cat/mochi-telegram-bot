"""All static configuration and config loading for the app.

Constants are declared here; runtime values (secrets, env vars, prompt texts)
are pulled through the loader functions below.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

import boto3

# ---------------------------------------------------------------------------
# External services
# ---------------------------------------------------------------------------

MOCHI_API_URL = "https://app.mochi.cards/api/cards/"
TELEGRAM_API_BASE = "https://api.telegram.org"

MOCHI_TIMEOUT_SECONDS = 15
TELEGRAM_TIMEOUT_SECONDS = 10
GEMINI_TIMEOUT_MS = 30_000

REQUIRED_SECRET_KEYS = (
    "MOCHI_API_KEY",
    "MOCHI_DECK_ID",
    "APP_SECRET",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
)

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

USAGE_ALIASES = {
    "n": "noun",
    "v": "verb",
    "adj": "adjective",
    "adv": "adverb",
    "phrasal verb": "phrasal-verb",
}

# DRAFT: generated but not sent anywhere yet. INBOX: parked, not in Mochi.
# RESERVED -> CREATED: being sent to / now in Mochi. FAILED: Mochi request failed.
STATUS_DRAFT = "DRAFT"
STATUS_INBOX = "INBOX"
STATUS_RESERVED = "RESERVED"
STATUS_CREATED = "CREATED"
STATUS_FAILED = "FAILED"

DESTINATION_MOCHI = "mochi"
DESTINATION_INBOX = "inbox"

# Card-generating commands and where they send the card. None = draft with buttons.
AI_COMMAND_DESTINATIONS = {
    "/ai": None,
    "/mochi": DESTINATION_MOCHI,
    "/inbox": DESTINATION_INBOX,
}

# Gemini's frequency estimate; anything else is dropped. Also used as Mochi tags.
FREQUENCY_VALUES = ("common", "moderate", "rare")
CEFR_LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")

# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------

INBOX_LIST_LIMIT = 20
# From this many sightings the card reply suggests moving the word to Mochi.
INBOX_SUGGEST_SEEN_COUNT = 3
# "Move seen 2+ to Mochi" button: threshold and words per press (Lambda timeout).
INBOX_PROMOTE_MIN_SEEN = 2
INBOX_PROMOTE_BATCH = 10

# /ai with several words (one per line or separated by ";"). Kept small so the
# whole batch fits the 30s Lambda timeout and Gemini rate limits.
BULK_AI_MAX_WORDS = 10
BULK_AI_MAX_WORKERS = 5
BULK_AI_SEPARATORS_PATTERN = r"[\n;]+"
# Leading list markers people paste: "- ", "* ", "• ", "1. ", "2) ".
BULK_AI_LIST_MARKER_PATTERN = r"^\s*(?:[-*•]|\d+[.)])\s+"

# ---------------------------------------------------------------------------
# Edit sessions
# ---------------------------------------------------------------------------

SESSION_TYPE_EDIT = "edit_card"
SESSION_STATUS_ACTIVE = "active"
EDIT_SESSION_TTL_SECONDS = 3600
LEGACY_CARD_SRS_ATTRIBUTES = (
    "review_count",
    "streak",
    "interval_days",
    "due_at",
    "last_result",
    "last_reviewed_at",
)

# ---------------------------------------------------------------------------
# User-facing texts
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Manual:\n"
    "/add reliable | надежный | adjective | This is a reliable source.\n"
    "\n"
    "AI draft (then pick Mochi or Inbox on the buttons):\n"
    "/ai reliable\n"
    "\n"
    "AI, straight to Mochi or Inbox:\n"
    "/mochi reliable\n"
    "/inbox reliable\n"
    "\n"
    "Several words: one per line or separated by \";\":\n"
    "/ai reliable\n"
    "take for granted\n"
    "so far this month\n"
    "\n"
    "Inbox list:\n"
    "/inbox\n"
    "\n"
    "Delete:\n"
    "/delete reliable\n"
    "\n"
    "Cancel edit:\n"
    "/cancel\n"
    "\n"
    "Regenerate / Edit / Delete are on the buttons under each card."
)

GENERIC_ERROR_TEXT = "Something went wrong. Please try again later."
AI_ERROR_TEXT = "AI could not complete this request. Please try again later."
BULK_AI_TOO_MANY_TEXT = "Up to {limit} words at a time, please. You sent {count}."

EDIT_CARD_PROMPT = (
    "Send the updated card in this format:\n\n"
    "word | translation | usage | example"
)

# Shown under help / unknown-command replies.
HELP_MENU_BUTTONS: tuple = ()

TELEGRAM_BOT_COMMANDS = (
    {"command": "add", "description": "Add a card draft manually"},
    {"command": "ai", "description": "Generate a card draft"},
    {"command": "mochi", "description": "Generate a card and send it to Mochi"},
    {"command": "inbox", "description": "Show inbox, or generate a card into it"},
    {"command": "delete", "description": "Delete a card"},
    {"command": "cancel", "description": "Cancel current edit"},
)

REGENERATE_BUTTON_TEXT = "Regenerate"
EDIT_BUTTON_TEXT = "Edit"
DELETE_BUTTON_TEXT = "Delete"
CONFIRM_DELETE_BUTTON_TEXT = "Yes, delete"
CANCEL_BUTTON_TEXT = "Cancel"
TO_MOCHI_BUTTON_TEXT = "To Mochi"
TO_INBOX_BUTTON_TEXT = "Inbox"
INBOX_WORD_TO_MOCHI_BUTTON_TEXT = "{word} → Mochi"
INBOX_PROMOTE_BUTTON_TEXT = "Move seen {min_seen}+ times to Mochi"

CALLBACK_REGEN_PREFIX = "regen:"
CALLBACK_EDIT_PREFIX = "edit:"
CALLBACK_DELETE_PREFIX = "del:"
CALLBACK_DELETE_CONFIRM_PREFIX = "delc:"
CALLBACK_CANCEL = "cancel"
CALLBACK_TO_MOCHI_PREFIX = "mochi:"
CALLBACK_TO_INBOX_PREFIX = "inbox:"
CALLBACK_INBOX_PROMOTE = "inbox_promote"
TELEGRAM_CALLBACK_DATA_MAX_BYTES = 64

WORD_NOT_FOUND_TEXT = "Word not found."
INBOX_EMPTY_TEXT = "Inbox is empty."
INBOX_NOTHING_TO_PROMOTE_TEXT = "No inbox words seen {min_seen}+ times."
INBOX_SUGGEST_TEXT = "Keeps coming up — maybe send it to Mochi?"
# seen_count in the meta line: how many times the word was added.
SEEN_COUNT_TEXT = "looked up {count} times"

# Just sent somewhere: one line "✅ word → Mochi" instead of the whole card.
ADDED_LINE = "✅ <b>{word}</b> → {destination}"
FAILED_LINE = "❌ <b>{word}</b> — failed, try again"
DESTINATION_LABELS = {
    STATUS_CREATED: "Mochi",
    STATUS_INBOX: "Inbox",
}
# Shown in a card's meta line; drafts have no label (their buttons say it all).
WORD_STATUS_LABELS = {
    STATUS_INBOX: "in inbox",
    STATUS_RESERVED: "sending to Mochi",
    STATUS_CREATED: "in Mochi",
    STATUS_FAILED: "Mochi request failed",
}
CANCELLED_TEXT = "Cancelled."

# Front side: only the word (H2 so it is not oversized). Back side: translation
# and example. Usage is stored only as a Mochi tag, never in the card text.
MOCHI_CARD_TEMPLATE = (
    "## {word}\n"
    "\n"
    "---\n"
    "\n"
    '<span style="color:#2563eb"><strong>{translation}</strong></span>\n'
    "\n"
    "**Example:**\n"
    "{example}"
)

# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"


@lru_cache(maxsize=None)
def load_prompt(name: str) -> str:
    """Prompt texts live in app/prompts/*.txt; {word}-style placeholders are replaced by the caller."""
    return (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def get_app_config() -> dict[str, str]:
    """
    In AWS: reads one JSON secret from Secrets Manager using APP_CONFIG_SECRET_ID.
    Locally: falls back to plain environment variables.
    """
    secret_id = os.environ.get("APP_CONFIG_SECRET_ID")

    if secret_id:
        client = boto3.client("secretsmanager")
        response = client.get_secret_value(SecretId=secret_id)

        secret_string = response.get("SecretString")
        if not secret_string:
            raise RuntimeError("SecretString is empty")

        config = json.loads(secret_string)
    else:
        config = {key: os.environ.get(key) for key in REQUIRED_SECRET_KEYS}

    missing = [key for key in REQUIRED_SECRET_KEYS if not config.get(key)]
    if missing:
        raise RuntimeError(f"Missing app config keys: {', '.join(missing)}")

    return {key: str(config[key]) for key in REQUIRED_SECRET_KEYS}


def get_known_words_table_name() -> str:
    table_name = os.environ.get("KNOWN_WORDS_TABLE_NAME")

    if not table_name:
        raise RuntimeError("KNOWN_WORDS_TABLE_NAME is not set")

    return table_name


def get_practice_sessions_table_name() -> str:
    table_name = os.environ.get("PRACTICE_SESSIONS_TABLE_NAME")

    if not table_name:
        raise RuntimeError("PRACTICE_SESSIONS_TABLE_NAME is not set")

    return table_name
