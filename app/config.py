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

STATUS_RESERVED = "RESERVED"
STATUS_CREATED = "CREATED"
STATUS_FAILED = "FAILED"

# ---------------------------------------------------------------------------
# Active practice
# ---------------------------------------------------------------------------

PRACTICE_MODE_TODAY = "today"
PRACTICE_SESSION_TYPE_ACTIVE = "active_practice"
PRACTICE_SESSION_TYPE_EDIT = "edit_card"
TASK_TYPE_FILL_BLANK = "fill_blank"
TASK_TYPE_TRANSLATE_RU_EN = "translate_ru_en"
TASK_TYPE_OWN_SENTENCE = "own_sentence"

PRACTICE_QUESTION_COUNT = 30
PRACTICE_BLOCK_QUESTION_COUNT = 10
PRACTICE_SCAN_LIMIT = 5_000
STATS_SCAN_LIMIT = 500
PRACTICE_SESSION_TTL_SECONDS = 3600
ANSWER_FUZZY_MATCH_THRESHOLD = 0.85

SESSION_STATUS_ACTIVE = "active"

RESULT_CORRECT = "correct"
RESULT_MINOR_ISSUE = "minor_issue"
RESULT_WRONG = "wrong"

# Lightweight scheduling for Telegram exercises only. Mochi remains the SRS.
PRACTICE_CORRECT_INTERVAL_STEPS = (1, 3, 7, 14, 30)
PRACTICE_MAX_PRIORITY_SCORE = 10
LEGACY_CARD_SRS_ATTRIBUTES = (
    "review_count",
    "streak",
    "interval_days",
    "due_at",
    "last_result",
    "last_reviewed_at",
)

CLOZE_BLANK = "_____"

# ---------------------------------------------------------------------------
# User-facing texts
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Manual:\n"
    "/add reliable | надежный | adjective | This is a reliable source.\n"
    "\n"
    "AI:\n"
    "/ai reliable\n"
    "\n"
    "Delete:\n"
    "/delete reliable\n"
    "\n"
    "Practice:\n"
    "/today\n"
    "\n"
    "Cancel edit/practice:\n"
    "/cancel\n"
    "\n"
    "Everything else is on the buttons below."
)

GENERIC_ERROR_TEXT = "Something went wrong. Please try again later."

NOT_ENOUGH_WORDS_TEXT = "Need at least 30 saved words with examples for active practice."
TODAY_PRACTICE_INTRO = "Today's active practice. Fill in the blanks:"
EDIT_CARD_PROMPT = (
    "Send the updated card in this format:\n\n"
    "word | translation | usage | example\n\n"
    "You can also add a fifth field for a cloze sentence."
)

# Shown under help / unknown-command replies.
HELP_MENU_BUTTONS = (
    (
        ("Today", "practice:today"),
        ("Stats", "practice:stats"),
    ),
)

TELEGRAM_BOT_COMMANDS = (
    {"command": "add", "description": "Add a card manually"},
    {"command": "ai", "description": "Generate and add a card"},
    {"command": "delete", "description": "Delete a card"},
    {"command": "today", "description": "Start active practice"},
    {"command": "stats", "description": "Show active practice stats"},
    {"command": "cancel", "description": "Cancel current edit or practice"},
)

REGENERATE_BUTTON_TEXT = "Regenerate"
EDIT_BUTTON_TEXT = "Edit"
DELETE_BUTTON_TEXT = "Delete"
CONFIRM_DELETE_BUTTON_TEXT = "Yes, delete"
CANCEL_BUTTON_TEXT = "Cancel"

CALLBACK_REGEN_PREFIX = "regen:"
CALLBACK_EDIT_PREFIX = "edit:"
CALLBACK_DELETE_PREFIX = "del:"
CALLBACK_DELETE_CONFIRM_PREFIX = "delc:"
CALLBACK_CANCEL = "cancel"
TELEGRAM_CALLBACK_DATA_MAX_BYTES = 64

WORD_NOT_FOUND_TEXT = "Word not found."
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
