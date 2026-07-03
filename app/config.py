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
# User-facing texts
# ---------------------------------------------------------------------------

HELP_TEXT = (
    "Manual:\n"
    "/add reliable | надежный | adjective | This is a reliable source.\n"
    "\n"
    "AI:\n"
    "/ai reliable"
)

GENERIC_ERROR_TEXT = "Something went wrong. Please try again later."

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
