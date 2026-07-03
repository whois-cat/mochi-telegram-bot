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
# Practice
# ---------------------------------------------------------------------------

PRACTICE_MODE_EN_RU = "en_ru"
PRACTICE_MODE_RU_EN = "ru_en"
PRACTICE_MODE_CLOZE = "cloze"
PRACTICE_MODE_WRITE_SENTENCE = "write_sentence"

PRACTICE_MODES = (
    PRACTICE_MODE_EN_RU,
    PRACTICE_MODE_RU_EN,
    PRACTICE_MODE_CLOZE,
    PRACTICE_MODE_WRITE_SENTENCE,
)

# Practice word sources.
PRACTICE_SOURCE_RANDOM = "random"
PRACTICE_SOURCE_DUE = "due"
PRACTICE_SOURCE_WEAK = "weak"

PRACTICE_QUESTION_COUNT = 10
PRACTICE_SCAN_LIMIT = 50
STATS_SCAN_LIMIT = 500
PRACTICE_SESSION_TTL_SECONDS = 3600

SESSION_STATUS_ACTIVE = "active"

RESULT_CORRECT = "correct"
RESULT_WRONG = "wrong"

# Simple spaced repetition: interval in days by streak; longer streaks cap out.
SRS_INTERVALS_BY_STREAK = {1: 1, 2: 3, 3: 7, 4: 14}
SRS_MAX_INTERVAL_DAYS = 30

# A word is "weak" when its correct rate falls below this threshold.
WEAK_CORRECT_RATE_THRESHOLD = 0.6

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
    "Everything else is on the buttons below."
)

GENERIC_ERROR_TEXT = "Something went wrong. Please try again later."

PRACTICE_MENU_TEXT = "Choose practice mode:"
NOT_ENOUGH_WORDS_TEXT = "Not enough saved words for this practice mode yet."
NO_DUE_WORDS_TEXT = "No words due today."
NO_WEAK_WORDS_TEXT = "No weak words yet."

# Inline keyboards as rows of (label, callback_data) pairs.
PRACTICE_MENU_BUTTONS = (
    (
        ("EN → RU", f"practice:{PRACTICE_MODE_EN_RU}"),
        ("RU → EN", f"practice:{PRACTICE_MODE_RU_EN}"),
    ),
    (
        ("Fill blanks x10", f"practice:{PRACTICE_MODE_CLOZE}"),
        ("Write sentence", f"practice:{PRACTICE_MODE_WRITE_SENTENCE}"),
    ),
    (
        ("Today", "practice:today"),
        ("Weak words", "practice:weak"),
    ),
)

# Shown under help / unknown-command replies.
HELP_MENU_BUTTONS = (
    (
        ("Practice", "practice:menu"),
        ("Stats", "practice:stats"),
    ),
    (
        ("Today", "practice:today"),
        ("Weak words", "practice:weak"),
    ),
)

REGENERATE_BUTTON_TEXT = "Regenerate"
CALLBACK_REGEN_PREFIX = "regen:"
TELEGRAM_CALLBACK_DATA_MAX_BYTES = 64

WORD_NOT_FOUND_TEXT = "Word not found."

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
