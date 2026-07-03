from __future__ import annotations

import base64
import hmac
import html
import json
import random
import re
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import boto3
import requests
from botocore.exceptions import ClientError

from app.config import (
    CALLBACK_REGEN_PREFIX,
    CLOZE_BLANK,
    GEMINI_TIMEOUT_MS,
    GENERIC_ERROR_TEXT,
    HELP_MENU_BUTTONS,
    HELP_TEXT,
    MOCHI_API_URL,
    MOCHI_CARD_TEMPLATE,
    MOCHI_TIMEOUT_SECONDS,
    NO_DUE_WORDS_TEXT,
    NO_WEAK_WORDS_TEXT,
    NOT_ENOUGH_WORDS_TEXT,
    PRACTICE_MENU_BUTTONS,
    PRACTICE_MENU_TEXT,
    PRACTICE_MODE_CLOZE,
    PRACTICE_MODE_EN_RU,
    PRACTICE_MODE_RU_EN,
    PRACTICE_MODE_WRITE_SENTENCE,
    PRACTICE_MODES,
    PRACTICE_QUESTION_COUNT,
    PRACTICE_SCAN_LIMIT,
    PRACTICE_SESSION_TTL_SECONDS,
    PRACTICE_SOURCE_DUE,
    PRACTICE_SOURCE_RANDOM,
    PRACTICE_SOURCE_WEAK,
    REGENERATE_BUTTON_TEXT,
    RESULT_CORRECT,
    RESULT_WRONG,
    SESSION_STATUS_ACTIVE,
    SRS_INTERVALS_BY_STREAK,
    SRS_MAX_INTERVAL_DAYS,
    STATS_SCAN_LIMIT,
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_RESERVED,
    TELEGRAM_API_BASE,
    TELEGRAM_CALLBACK_DATA_MAX_BYTES,
    TELEGRAM_TIMEOUT_SECONDS,
    USAGE_ALIASES,
    WEAK_CORRECT_RATE_THRESHOLD,
    WORD_NOT_FOUND_TEXT,
    get_app_config,
    get_known_words_table_name,
    get_practice_sessions_table_name,
    load_prompt,
)

try:
    from google import genai
    from google.genai import types as genai_types
except ImportError:  # pragma: no cover - SDK missing only in local dev
    genai = None
    genai_types = None


# ---------------------------------------------------------------------------
# Exceptions and result types
# ---------------------------------------------------------------------------


class HttpError(Exception):
    """Maps to an HTTP error response from the Lambda handler."""

    def __init__(self, status_code: int, message: str) -> None:
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class UserInputError(Exception):
    """User-facing input error; its message is safe to send back to Telegram."""


class AiGenerationError(Exception):
    """Gemini failed or returned an invalid card; safe generic reply is sent."""


@dataclass(frozen=True)
class CardInput:
    word: str
    translation: str
    usage: str
    example: str


@dataclass(frozen=True)
class AddCardResult:
    created: bool
    word: str
    mochi_card_id: str | None = None
    translation: str | None = None
    example: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "created": self.created,
            "word": self.word,
            "mochi_card_id": self.mochi_card_id,
            "message": "Card added to Mochi" if self.created else "Card already exists",
        }


@dataclass(frozen=True)
class TelegramCommand:
    mode: str  # "manual" | "ai" | "help" | "practice_menu" | "practice_today" | "practice_weak" | "stats"
    card: CardInput | None = None
    query: str | None = None


# ---------------------------------------------------------------------------
# Safe logging
# ---------------------------------------------------------------------------


def log_event(message: str, **fields: Any) -> None:
    """Structured log line. Never pass secrets, headers, bodies or payloads."""
    print(json.dumps({"message": message, **fields}, ensure_ascii=False))


def log_safe_request(event: dict[str, Any]) -> None:
    request_context = event.get("requestContext") or {}
    http_context = request_context.get("http") or {}

    log_event(
        "request_received",
        method=http_context.get("method"),
        path=event.get("rawPath") or event.get("path"),
        request_id=request_context.get("requestId"),
    )


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------


def json_response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def get_header(event: dict[str, Any], header_name: str) -> str | None:
    headers = event.get("headers") or {}
    target = header_name.lower()

    for key, value in headers.items():
        if key.lower() == target:
            return str(value)

    return None


def require_secret_header(
    event: dict[str, Any],
    header_name: str,
    expected_secret: str,
) -> None:
    provided_secret = get_header(event, header_name)

    if not provided_secret or not hmac.compare_digest(provided_secret, expected_secret):
        raise HttpError(401, "Unauthorized")


def parse_json_body(event: dict[str, Any]) -> dict[str, Any]:
    raw_body = event.get("body")

    if not raw_body:
        raise HttpError(400, "Request body is required")

    if event.get("isBase64Encoded"):
        raw_body = base64.b64decode(raw_body).decode("utf-8")

    try:
        parsed = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HttpError(400, "Request body must be valid JSON")

    if not isinstance(parsed, dict):
        raise HttpError(400, "Request body must be a JSON object")

    return parsed


def require_string_field(data: dict[str, Any], field_name: str) -> str:
    value = data.get(field_name)

    if not isinstance(value, str) or not value.strip():
        raise HttpError(400, f"Field '{field_name}' is required")

    return value.strip()


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def normalize_usage_tag(usage: str) -> str:
    normalized = " ".join(usage.strip().lower().split())
    return USAGE_ALIASES.get(normalized, normalized.replace(" ", "-"))


def normalize_word_for_lookup(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    return " ".join(normalized.split())


def build_word_key(*, deck_id: str, normalized_word: str) -> str:
    return f"deck#{deck_id}#word#{normalized_word}"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DynamoDB helpers
# ---------------------------------------------------------------------------


def get_known_words_table():
    return boto3.resource("dynamodb").Table(get_known_words_table_name())


def word_key_for(word: str, config: dict[str, str]) -> str:
    return build_word_key(
        deck_id=config["MOCHI_DECK_ID"],
        normalized_word=normalize_word_for_lookup(word),
    )


def get_known_word(word: str, config: dict[str, str]) -> dict[str, Any] | None:
    response = get_known_words_table().get_item(
        Key={"word_key": word_key_for(word, config)},
        ConsistentRead=True,
    )
    return response.get("Item")


def reserve_known_word(
    *,
    word: str,
    translation: str,
    example: str,
    usage_tag: str,
    source: str,
    config: dict[str, str],
) -> dict[str, Any]:
    """
    Atomically reserve the word. Succeeds when the item does not exist or its
    previous attempt FAILED (retry). Returns {"reserved": bool, "item": dict}.
    """
    table = get_known_words_table()
    normalized_word = normalize_word_for_lookup(word)
    word_key = build_word_key(
        deck_id=config["MOCHI_DECK_ID"],
        normalized_word=normalized_word,
    )
    now = utc_now_iso()

    item = {
        "word_key": word_key,
        "deck_id": config["MOCHI_DECK_ID"],
        "normalized_word": normalized_word,
        "word": word,
        "translation": translation,
        "example": example,
        "usage_tag": usage_tag,
        "source": source,
        "status": STATUS_RESERVED,
        "created_at": now,
        "updated_at": now,
        "review_count": 0,
        "correct_count": 0,
        "wrong_count": 0,
        "streak": 0,
        "interval_days": 0,
        "due_at": now,
    }

    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(word_key) OR #status = :failed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":failed": STATUS_FAILED},
        )
        return {"reserved": True, "item": item}

    except ClientError as error:
        error_code = error.response.get("Error", {}).get("Code")
        if error_code != "ConditionalCheckFailedException":
            raise

        existing = table.get_item(
            Key={"word_key": word_key},
            ConsistentRead=True,
        ).get("Item")

        return {"reserved": False, "item": existing or {"word_key": word_key, "word": word}}


def mark_known_word_created(*, word_key: str, mochi_card_id: str | None) -> None:
    get_known_words_table().update_item(
        Key={"word_key": word_key},
        UpdateExpression="SET #status = :status, mochi_card_id = :card_id, updated_at = :updated_at",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": STATUS_CREATED,
            ":card_id": mochi_card_id or "",
            ":updated_at": utc_now_iso(),
        },
    )


def mark_known_word_failed(*, word_key: str, error_message: str) -> None:
    get_known_words_table().update_item(
        Key={"word_key": word_key},
        UpdateExpression="SET #status = :status, last_error = :last_error, updated_at = :updated_at",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={
            ":status": STATUS_FAILED,
            ":last_error": error_message[:300],
            ":updated_at": utc_now_iso(),
        },
    )


# ---------------------------------------------------------------------------
# Mochi helpers
# ---------------------------------------------------------------------------


def build_mochi_content(card: CardInput) -> str:
    return MOCHI_CARD_TEMPLATE.format(
        word=card.word,
        translation=card.translation,
        example=card.example,
    )


def create_mochi_card(card: CardInput, usage_tag: str, config: dict[str, str]) -> dict[str, Any]:
    payload = {
        "deck-id": config["MOCHI_DECK_ID"],
        "content": build_mochi_content(card),
        "manual-tags": ["english", "from-telegram", usage_tag],
        "archived?": False,
        "review-reverse?": False,
    }

    response = requests.post(
        MOCHI_API_URL,
        json=payload,
        auth=(config["MOCHI_API_KEY"], ""),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=MOCHI_TIMEOUT_SECONDS,
    )

    if response.status_code >= 400:
        log_event(
            "mochi_api_error",
            status_code=response.status_code,
            response_preview=response.text[:300],
        )
        raise HttpError(502, "Mochi API request failed")

    try:
        return response.json()
    except ValueError:
        return {}


def update_mochi_card(
    card_id: str,
    card: CardInput,
    usage_tag: str,
    config: dict[str, str],
) -> None:
    payload = {
        "content": build_mochi_content(card),
        "manual-tags": ["english", "from-telegram", usage_tag],
    }

    response = requests.post(
        f"{MOCHI_API_URL}{card_id}",
        json=payload,
        auth=(config["MOCHI_API_KEY"], ""),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=MOCHI_TIMEOUT_SECONDS,
    )

    if response.status_code >= 400:
        log_event(
            "mochi_api_error",
            status_code=response.status_code,
            response_preview=response.text[:300],
        )
        raise HttpError(502, "Mochi API request failed")


# ---------------------------------------------------------------------------
# Gemini helpers
# ---------------------------------------------------------------------------


def generate_card_with_ai(word_or_phrase: str, config: dict[str, str]) -> CardInput:
    """
    Ask Gemini for a full card as strict JSON and validate it.
    All Gemini-specific code lives here.
    """
    if genai is None:
        log_event("gemini_sdk_missing")
        raise AiGenerationError("google-genai SDK is not installed")

    client = genai.Client(
        api_key=config["GEMINI_API_KEY"],
        http_options=genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )

    try:
        response = client.models.generate_content(
            model=config["GEMINI_MODEL"],
            contents=load_prompt("gemini_card").replace("{word}", word_or_phrase),
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
    except Exception as error:
        log_event("gemini_request_error", error_type=type(error).__name__)
        raise AiGenerationError("Gemini request failed") from error

    raw_text = (response.text or "").strip()

    # Defensive: strip accidental markdown code fences.
    if raw_text.startswith("```"):
        raw_text = raw_text.strip("`")
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        log_event("gemini_invalid_json", response_preview=raw_text[:120])
        raise AiGenerationError("Gemini returned invalid JSON")

    if not isinstance(data, dict):
        log_event("gemini_invalid_shape")
        raise AiGenerationError("Gemini returned a non-object response")

    fields: dict[str, str] = {}
    for field_name in ("word", "translation", "usage", "example"):
        value = data.get(field_name)
        if not isinstance(value, str) or not value.strip():
            log_event("gemini_missing_field", field=field_name)
            raise AiGenerationError(f"Gemini response is missing '{field_name}'")
        fields[field_name] = value.strip()

    return CardInput(**fields)


def evaluate_sentence_with_gemini(
    word: str,
    user_sentence: str,
    config: dict[str, str],
) -> dict[str, str]:
    """
    Ask Gemini to judge a learner's sentence for the given word.
    Returns {"result": "good"|"minor_issue"|"wrong_usage", "feedback": ..., "better_sentence": ...}.
    """
    if genai is None:
        log_event("gemini_sdk_missing")
        raise AiGenerationError("google-genai SDK is not installed")

    client = genai.Client(
        api_key=config["GEMINI_API_KEY"],
        http_options=genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )

    prompt = (
        load_prompt("gemini_sentence_eval")
        .replace("{word}", word)
        .replace("{sentence}", user_sentence)
    )

    try:
        response = client.models.generate_content(
            model=config["GEMINI_MODEL"],
            contents=prompt,
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
            ),
        )
    except Exception as error:
        log_event("gemini_request_error", error_type=type(error).__name__)
        raise AiGenerationError("Gemini request failed") from error

    raw_text = (response.text or "").strip()

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        log_event("gemini_invalid_json", response_preview=raw_text[:120])
        raise AiGenerationError("Gemini returned invalid JSON")

    if not isinstance(data, dict) or data.get("result") not in ("good", "minor_issue", "wrong_usage"):
        log_event("gemini_invalid_shape")
        raise AiGenerationError("Gemini returned an invalid evaluation")

    return {
        "result": str(data["result"]),
        "feedback": str(data.get("feedback") or "").strip(),
        "better_sentence": str(data.get("better_sentence") or "").strip(),
    }


# ---------------------------------------------------------------------------
# Central add-card flow
# ---------------------------------------------------------------------------


def add_card(card: CardInput, config: dict[str, str], *, source: str = "telegram") -> AddCardResult:
    """
    Single flow used by POST /add-card, Telegram /add, and Telegram /ai:
    reserve in DynamoDB, create the Mochi card, then confirm or mark FAILED.
    """
    usage_tag = normalize_usage_tag(card.usage)

    reservation = reserve_known_word(
        word=card.word,
        translation=card.translation,
        example=card.example,
        usage_tag=usage_tag,
        source=source,
        config=config,
    )

    if not reservation["reserved"]:
        existing = reservation["item"]
        return AddCardResult(
            created=False,
            word=existing.get("word") or card.word,
            mochi_card_id=existing.get("mochi_card_id") or None,
            translation=existing.get("translation") or None,
            example=existing.get("example") or None,
        )

    word_key = reservation["item"]["word_key"]

    try:
        mochi_card = create_mochi_card(card, usage_tag, config)
    except Exception as error:
        mark_known_word_failed(word_key=word_key, error_message=str(error))
        raise

    mochi_card_id = mochi_card.get("id")
    mark_known_word_created(word_key=word_key, mochi_card_id=mochi_card_id)

    return AddCardResult(
        created=True,
        word=card.word,
        mochi_card_id=mochi_card_id,
        translation=card.translation,
        example=card.example,
    )


def add_ai_card(word_or_phrase: str, config: dict[str, str]) -> AddCardResult:
    """
    AI flow: check DynamoDB first so duplicates never spend a Gemini call,
    then generate the card and push it through the central add_card() flow.
    """
    existing = get_known_word(word_or_phrase, config)

    if existing and existing.get("status") in (STATUS_CREATED, STATUS_RESERVED):
        return AddCardResult(
            created=False,
            word=existing.get("word") or word_or_phrase,
            mochi_card_id=existing.get("mochi_card_id") or None,
            translation=existing.get("translation") or None,
            example=existing.get("example") or None,
        )

    card = generate_card_with_ai(word_or_phrase, config)
    return add_card(card, config, source="telegram")


def regenerate_card(normalized_word: str, config: dict[str, str]) -> AddCardResult:
    """
    Regenerate translation/example with Gemini for an already saved word,
    updating both the Mochi card (when it exists) and the DynamoDB item.
    """
    table = get_known_words_table()
    word_key = build_word_key(
        deck_id=config["MOCHI_DECK_ID"],
        normalized_word=normalized_word,
    )
    item = table.get_item(Key={"word_key": word_key}, ConsistentRead=True).get("Item")

    if not item:
        raise UserInputError(WORD_NOT_FOUND_TEXT)

    word = str(item.get("word") or normalized_word)
    card = generate_card_with_ai(word, config)
    usage_tag = normalize_usage_tag(card.usage)

    mochi_card_id = item.get("mochi_card_id")
    if mochi_card_id:
        update_mochi_card(str(mochi_card_id), card, usage_tag, config)

    table.update_item(
        Key={"word_key": word_key},
        # "translation" is a DynamoDB reserved keyword, so alias it.
        UpdateExpression=(
            "SET #translation = :translation, example = :example, "
            "usage_tag = :usage_tag, updated_at = :updated_at"
        ),
        ExpressionAttributeNames={"#translation": "translation"},
        ExpressionAttributeValues={
            ":translation": card.translation,
            ":example": card.example,
            ":usage_tag": usage_tag,
            ":updated_at": utc_now_iso(),
        },
    )

    log_event("card_regenerated", has_mochi_card=bool(mochi_card_id))

    return AddCardResult(
        created=True,
        word=word,
        mochi_card_id=str(mochi_card_id) if mochi_card_id else None,
        translation=card.translation,
        example=card.example,
    )


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def send_telegram_message(
    *,
    chat_id: int | str,
    text: str,
    config: dict[str, str],
    reply_markup: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup

    response = requests.post(
        f"{TELEGRAM_API_BASE}/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage",
        json=payload,
        timeout=TELEGRAM_TIMEOUT_SECONDS,
    )

    if response.status_code >= 400:
        log_event(
            "telegram_send_message_error",
            status_code=response.status_code,
            response_preview=response.text[:300],
        )


def answer_callback_query(callback_query_id: str, config: dict[str, str]) -> None:
    response = requests.post(
        f"{TELEGRAM_API_BASE}/bot{config['TELEGRAM_BOT_TOKEN']}/answerCallbackQuery",
        json={"callback_query_id": callback_query_id},
        timeout=TELEGRAM_TIMEOUT_SECONDS,
    )

    if response.status_code >= 400:
        log_event(
            "telegram_answer_callback_error",
            status_code=response.status_code,
            response_preview=response.text[:300],
        )


def build_inline_keyboard(rows: tuple) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in rows
        ]
    }


def build_practice_menu() -> dict[str, Any]:
    return build_inline_keyboard(PRACTICE_MENU_BUTTONS)


def build_help_menu() -> dict[str, Any]:
    return build_inline_keyboard(HELP_MENU_BUTTONS)


def build_regenerate_keyboard(word: str) -> dict[str, Any] | None:
    """None when the word does not fit Telegram's callback_data size limit."""
    data = CALLBACK_REGEN_PREFIX + normalize_word_for_lookup(word)

    if len(data.encode("utf-8")) > TELEGRAM_CALLBACK_DATA_MAX_BYTES:
        return None

    return {"inline_keyboard": [[{"text": REGENERATE_BUTTON_TEXT, "callback_data": data}]]}


def parse_telegram_command(text: str) -> TelegramCommand:
    """
    Supported:
      /add word | translation | usage | example  -> mode="manual"
      /ai word_or_phrase                         -> mode="ai"
      /help, /start                              -> mode="help"
      /practice                                  -> mode="practice_menu"
      /today                                     -> mode="practice_today"
      /weak                                      -> mode="practice_weak"
      /stats                                     -> mode="stats"
    Anything else raises UserInputError with the format help.
    """
    text = text.strip()
    command, _, rest = text.partition(" ")
    command = command.split("@", 1)[0].lower()  # supports /add@YourBotName
    rest = rest.strip()

    if command in ("/help", "/start"):
        return TelegramCommand(mode="help")

    if command == "/practice":
        return TelegramCommand(mode="practice_menu")

    if command == "/today":
        return TelegramCommand(mode="practice_today")

    if command == "/weak":
        return TelegramCommand(mode="practice_weak")

    if command == "/stats":
        return TelegramCommand(mode="stats")

    if command == "/add":
        parts = [part.strip() for part in rest.split("|")]
        if len(parts) != 4 or any(not part for part in parts):
            raise UserInputError(f"Please use this format:\n\n{HELP_TEXT}")

        word, translation, usage, example = parts
        return TelegramCommand(
            mode="manual",
            card=CardInput(word=word, translation=translation, usage=usage, example=example),
        )

    if command == "/ai":
        if not rest:
            raise UserInputError(f"Please add a word or phrase:\n\n{HELP_TEXT}")
        return TelegramCommand(mode="ai", query=rest)

    raise UserInputError(f"I don't know that command.\n\n{HELP_TEXT}")


def extract_telegram_message(update: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "edited_message"):
        message = update.get(key)
        if isinstance(message, dict):
            return message
    return None


def build_add_result_reply(
    result: AddCardResult,
    *,
    title: str | None = None,
) -> tuple[str, dict[str, Any] | None]:
    """Card summary with what goes to Mochi, plus a Regenerate button."""
    word = html.escape(result.word)
    status = title or ("✅ Added" if result.created else "🔁 Exists")

    lines = [f"{status}: <b>{word}</b>"]
    if result.translation:
        lines += ["", f"<b>{html.escape(result.translation)}</b>"]
    if result.example:
        lines.append(f"<i>{html.escape(result.example)}</i>")

    return "\n".join(lines), build_regenerate_keyboard(result.word)


def process_telegram_text(
    text: str,
    config: dict[str, str],
    *,
    telegram_user_id: str,
) -> tuple[str, dict[str, Any] | None]:
    """Returns (reply_text, reply_markup or None)."""
    stripped = text.strip()

    # Plain text while a practice session is active is the session answer.
    if not stripped.startswith("/"):
        session = get_active_practice_session(telegram_user_id)
        if session:
            return handle_practice_answer(session, stripped, config), None

    command = parse_telegram_command(stripped)
    log_event("telegram_command", mode=command.mode)

    if command.mode == "help":
        return HELP_TEXT, build_help_menu()

    if command.mode == "practice_menu":
        return PRACTICE_MENU_TEXT, build_practice_menu()

    if command.mode == "practice_today":
        return start_practice_session(
            telegram_user_id, PRACTICE_MODE_EN_RU, source=PRACTICE_SOURCE_DUE
        ), None

    if command.mode == "practice_weak":
        return start_practice_session(
            telegram_user_id, PRACTICE_MODE_EN_RU, source=PRACTICE_SOURCE_WEAK
        ), None

    if command.mode == "stats":
        return build_stats_reply(), None

    if command.mode == "manual":
        return build_add_result_reply(add_card(command.card, config, source="telegram"))

    return build_add_result_reply(add_ai_card(command.query, config))


# ---------------------------------------------------------------------------
# Practice: word selection
# ---------------------------------------------------------------------------


def get_known_words_for_practice(limit: int = PRACTICE_SCAN_LIMIT) -> list[dict[str, Any]]:
    """Scan is fine here: small personal table (see project constraints)."""
    response = get_known_words_table().scan(Limit=limit)
    items = response.get("Items") or []
    return [item for item in items if item.get("status") == STATUS_CREATED]


def is_due_word(item: dict[str, Any], now_iso: str) -> bool:
    due_at = item.get("due_at")
    return not due_at or str(due_at) <= now_iso


def is_weak_word(item: dict[str, Any]) -> bool:
    if item.get("last_result") == RESULT_WRONG:
        return True

    if int(item.get("wrong_count") or 0) > 0:
        return True

    review_count = int(item.get("review_count") or 0)
    correct_count = int(item.get("correct_count") or 0)
    return review_count > 0 and correct_count / review_count < WEAK_CORRECT_RATE_THRESHOLD


def get_due_words(limit: int = PRACTICE_QUESTION_COUNT) -> list[dict[str, Any]]:
    now_iso = utc_now_iso()
    due = [item for item in get_known_words_for_practice() if is_due_word(item, now_iso)]
    return due[:limit]


def get_weak_words(limit: int = PRACTICE_QUESTION_COUNT) -> list[dict[str, Any]]:
    weak = [item for item in get_known_words_for_practice() if is_weak_word(item)]
    return weak[:limit]


def build_cloze_sentence(word: str, example: str) -> str | None:
    """Blank the target word out of its example; None when it cannot be blanked."""
    if not word or not example:
        return None

    blanked, count = re.subn(re.escape(word), CLOZE_BLANK, example, flags=re.IGNORECASE)
    return blanked if count else None


def word_fits_mode(item: dict[str, Any], mode: str) -> bool:
    if not item.get("word"):
        return False

    if mode in (PRACTICE_MODE_EN_RU, PRACTICE_MODE_RU_EN):
        return bool(item.get("translation"))

    if mode == PRACTICE_MODE_CLOZE:
        return build_cloze_sentence(str(item.get("word")), str(item.get("example") or "")) is not None

    return True  # write_sentence needs only the word


def choose_practice_words(
    mode: str,
    source: str,
    limit: int = PRACTICE_QUESTION_COUNT,
) -> list[dict[str, Any]]:
    if source == PRACTICE_SOURCE_DUE:
        words = get_due_words(limit=PRACTICE_SCAN_LIMIT)
    elif source == PRACTICE_SOURCE_WEAK:
        words = get_weak_words(limit=PRACTICE_SCAN_LIMIT)
    else:
        words = get_known_words_for_practice()

    words = [item for item in words if word_fits_mode(item, mode)]
    random.shuffle(words)
    return words[:limit]


# ---------------------------------------------------------------------------
# Practice: sessions
# ---------------------------------------------------------------------------


def get_practice_sessions_table():
    return boto3.resource("dynamodb").Table(get_practice_sessions_table_name())


def save_practice_session(telegram_user_id: str, session: dict[str, Any]) -> None:
    get_practice_sessions_table().put_item(Item=session)


def get_active_practice_session(telegram_user_id: str) -> dict[str, Any] | None:
    item = (
        get_practice_sessions_table()
        .get_item(Key={"telegram_user_id": str(telegram_user_id)}, ConsistentRead=True)
        .get("Item")
    )

    if not item or item.get("status") != SESSION_STATUS_ACTIVE:
        return None

    # DynamoDB TTL deletion is lazy; treat expired sessions as gone.
    if int(item.get("expires_at") or 0) <= time.time():
        return None

    return item


def clear_practice_session(telegram_user_id: str) -> None:
    get_practice_sessions_table().delete_item(Key={"telegram_user_id": str(telegram_user_id)})


def build_practice_questions(mode: str, words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions = []

    for item in words:
        word = str(item.get("word") or "")
        translation = str(item.get("translation") or "")
        example = str(item.get("example") or "")

        if mode == PRACTICE_MODE_EN_RU:
            prompt, expected = word, translation
        elif mode == PRACTICE_MODE_RU_EN:
            prompt, expected = translation, word
        elif mode == PRACTICE_MODE_CLOZE:
            blanked = build_cloze_sentence(word, example)
            if not blanked:
                continue
            prompt, expected = blanked, word
        else:  # write_sentence
            prompt, expected = word, ""

        questions.append(
            {
                "word_key": item["word_key"],
                "word": word,
                "translation": translation,
                "example": example,
                "prompt": prompt,
                "expected_answer": expected,
            }
        )

    return questions


def build_practice_prompt(mode: str, questions: list[dict[str, Any]]) -> str:
    if mode == PRACTICE_MODE_WRITE_SENTENCE:
        return f"Write your own sentence with:\n\n<b>{html.escape(questions[0]['word'])}</b>"

    headers = {
        PRACTICE_MODE_EN_RU: "Translate to Russian:",
        PRACTICE_MODE_RU_EN: "Translate to English:",
        PRACTICE_MODE_CLOZE: "Fill in the blanks:",
    }
    lines = [headers[mode], ""]
    lines += [f"{number}. {html.escape(q['prompt'])}" for number, q in enumerate(questions, 1)]
    return "\n".join(lines)


def start_practice_session(
    telegram_user_id: str,
    mode: str,
    *,
    source: str = PRACTICE_SOURCE_RANDOM,
) -> str:
    """Create and save a session, returning the message to send to the chat."""
    limit = 1 if mode == PRACTICE_MODE_WRITE_SENTENCE else PRACTICE_QUESTION_COUNT
    words = choose_practice_words(mode, source, limit=limit)
    questions = build_practice_questions(mode, words)

    if not questions:
        if source == PRACTICE_SOURCE_DUE:
            return NO_DUE_WORDS_TEXT
        if source == PRACTICE_SOURCE_WEAK:
            return NO_WEAK_WORDS_TEXT
        return NOT_ENOUGH_WORDS_TEXT

    session = {
        "telegram_user_id": str(telegram_user_id),
        "mode": mode,
        "questions": questions,
        "created_at": utc_now_iso(),
        "expires_at": int(time.time()) + PRACTICE_SESSION_TTL_SECONDS,
        "status": SESSION_STATUS_ACTIVE,
    }
    save_practice_session(str(telegram_user_id), session)

    log_event("practice_session_started", mode=mode, source=source, questions=len(questions))
    return build_practice_prompt(mode, questions)


# ---------------------------------------------------------------------------
# Practice: spaced repetition
# ---------------------------------------------------------------------------


def calculate_next_review(is_correct: bool, current_streak: int) -> tuple[int, int]:
    """Returns (new_streak, interval_days)."""
    if not is_correct:
        return 0, 1

    streak = current_streak + 1
    return streak, SRS_INTERVALS_BY_STREAK.get(streak, SRS_MAX_INTERVAL_DAYS)


def update_word_review_stats(word_key: str, is_correct: bool) -> None:
    table = get_known_words_table()
    item = table.get_item(Key={"word_key": word_key}).get("Item") or {}
    streak, interval_days = calculate_next_review(is_correct, int(item.get("streak") or 0))
    now = datetime.now(timezone.utc)

    table.update_item(
        Key={"word_key": word_key},
        UpdateExpression=(
            "SET review_count = if_not_exists(review_count, :zero) + :one, "
            "correct_count = if_not_exists(correct_count, :zero) + :correct, "
            "wrong_count = if_not_exists(wrong_count, :zero) + :wrong, "
            "last_reviewed_at = :now, last_result = :result, "
            "streak = :streak, interval_days = :interval, "
            "due_at = :due_at, updated_at = :now"
        ),
        ExpressionAttributeValues={
            ":zero": 0,
            ":one": 1,
            ":correct": 1 if is_correct else 0,
            ":wrong": 0 if is_correct else 1,
            ":now": now.isoformat(),
            ":result": RESULT_CORRECT if is_correct else RESULT_WRONG,
            ":streak": streak,
            ":interval": interval_days,
            ":due_at": (now + timedelta(days=interval_days)).isoformat(),
        },
    )


# ---------------------------------------------------------------------------
# Practice: answer evaluation
# ---------------------------------------------------------------------------

NUMBERED_ANSWER_RE = re.compile(r"^\s*(\d{1,2})\s*[.):\-]?\s+(.+)$")


def parse_numbered_answers(text: str) -> dict[int, str]:
    """
    Parse '1. ответ' / '2) ответ' style lines. When no line is numbered,
    non-empty lines are taken in order as answers 1..N.
    """
    numbered: dict[int, str] = {}
    plain: list[str] = []

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue

        match = NUMBERED_ANSWER_RE.match(line)
        if match:
            numbered[int(match.group(1))] = match.group(2).strip()
        else:
            plain.append(line)

    if numbered:
        return numbered

    return {number: line for number, line in enumerate(plain, 1)}


def normalize_answer(value: str) -> str:
    return value.strip().strip(".,!?;:'\"()«»—–-").strip().lower()


def answer_matches(expected: str, answer: str, *, allow_substring: bool) -> bool:
    expected_norm = normalize_answer(expected)
    answer_norm = normalize_answer(answer)

    if not answer_norm or not expected_norm:
        return False

    if answer_norm == expected_norm:
        return True

    # Stored translations may hold several options ("надежный, достоверный").
    return allow_substring and (answer_norm in expected_norm or expected_norm in answer_norm)


def evaluate_numbered_answers(
    session: dict[str, Any],
    user_text: str,
    *,
    allow_substring: bool,
) -> str:
    questions = session.get("questions") or []
    answers = parse_numbered_answers(user_text)

    lines = []
    correct = 0

    for number, question in enumerate(questions, 1):
        answer = answers.get(number, "")
        expected = str(question.get("expected_answer") or "")
        is_correct = answer_matches(expected, answer, allow_substring=allow_substring)
        update_word_review_stats(str(question["word_key"]), is_correct)

        if is_correct:
            correct += 1
            lines.append(f"{number}. ✓ {html.escape(expected)}")
        else:
            given = html.escape(answer) if answer else "no answer"
            lines.append(f"{number}. ✗ {given} (correct: {html.escape(expected)})")

    lines.append("")
    lines.append(f"Score: {correct}/{len(questions)}")
    return "\n".join(lines)


def evaluate_translation_answers(session: dict[str, Any], user_text: str) -> str:
    # Substring matching only helps when the expected answer is a Russian
    # translation with several comma-separated options (EN → RU).
    allow_substring = session.get("mode") == PRACTICE_MODE_EN_RU
    return evaluate_numbered_answers(session, user_text, allow_substring=allow_substring)


def evaluate_cloze_answers(session: dict[str, Any], user_text: str) -> str:
    return evaluate_numbered_answers(session, user_text, allow_substring=False)


def evaluate_sentence_answer(
    session: dict[str, Any],
    user_text: str,
    config: dict[str, str],
) -> str:
    question = (session.get("questions") or [{}])[0]
    evaluation = evaluate_sentence_with_gemini(str(question.get("word") or ""), user_text, config)

    result = evaluation["result"]
    is_correct = result in ("good", "minor_issue")
    update_word_review_stats(str(question["word_key"]), is_correct)

    lines = []
    if result == "good":
        lines.append("Good sentence.")
    elif result == "minor_issue":
        lines.append("Minor issue.")
    else:
        lines.append("Wrong usage.")

    if evaluation["feedback"]:
        lines.append(html.escape(evaluation["feedback"]))
    if result != "good" and evaluation["better_sentence"]:
        lines.append(f"Better: {html.escape(evaluation['better_sentence'])}")

    return "\n".join(lines)


def handle_practice_answer(
    session: dict[str, Any],
    user_text: str,
    config: dict[str, str],
) -> str:
    mode = session.get("mode")

    if mode == PRACTICE_MODE_WRITE_SENTENCE:
        reply = evaluate_sentence_answer(session, user_text, config)
    elif mode == PRACTICE_MODE_CLOZE:
        reply = evaluate_cloze_answers(session, user_text)
    else:
        reply = evaluate_translation_answers(session, user_text)

    # Cleared only after successful evaluation so the user can retry on errors.
    clear_practice_session(str(session["telegram_user_id"]))
    return reply


# ---------------------------------------------------------------------------
# Practice: stats
# ---------------------------------------------------------------------------


def build_stats_reply() -> str:
    words = get_known_words_for_practice(limit=STATS_SCAN_LIMIT)
    now_iso = utc_now_iso()

    reviewed = sum(1 for item in words if int(item.get("review_count") or 0) > 0)
    due = sum(1 for item in words if is_due_word(item, now_iso))
    weak = sum(1 for item in words if is_weak_word(item))

    return (
        f"Total words: {len(words)}\n"
        f"Reviewed words: {reviewed}\n"
        f"Due today: {due}\n"
        f"Weak words: {weak}"
    )


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


def handle_health() -> dict[str, Any]:
    return json_response(200, {"ok": True, "message": "Mochi Telegram bot Lambda is healthy"})


def handle_add_card(event: dict[str, Any]) -> dict[str, Any]:
    config = get_app_config()
    require_secret_header(event, "x-bot-secret", config["APP_SECRET"])

    data = parse_json_body(event)
    card = CardInput(
        word=require_string_field(data, "word"),
        translation=require_string_field(data, "translation"),
        usage=require_string_field(data, "usage"),
        example=require_string_field(data, "example"),
    )

    result = add_card(card, config, source="api")
    return json_response(200, result.to_dict())


def dispatch_callback(data: str, user_id: str, config: dict[str, str]) -> tuple[str, dict[str, Any] | None] | None:
    """Returns (reply, reply_markup) for known callback_data, None for unknown."""
    if data.startswith(CALLBACK_REGEN_PREFIX):
        result = regenerate_card(data[len(CALLBACK_REGEN_PREFIX):], config)
        return build_add_result_reply(result, title="Regenerated")

    if not data.startswith("practice:"):
        return None

    choice = data.split(":", 1)[1]
    log_event("practice_callback", choice=choice)

    if choice == "menu":
        return PRACTICE_MENU_TEXT, build_practice_menu()

    if choice == "stats":
        return build_stats_reply(), None

    if choice == "today":
        return start_practice_session(user_id, PRACTICE_MODE_EN_RU, source=PRACTICE_SOURCE_DUE), None

    if choice == "weak":
        return start_practice_session(user_id, PRACTICE_MODE_EN_RU, source=PRACTICE_SOURCE_WEAK), None

    if choice in PRACTICE_MODES:
        return start_practice_session(user_id, choice), None

    return None


def handle_callback_query(callback_query: dict[str, Any], config: dict[str, str]) -> None:
    callback_query_id = callback_query.get("id")
    if callback_query_id:
        answer_callback_query(str(callback_query_id), config)

    chat_id = ((callback_query.get("message") or {}).get("chat") or {}).get("id")
    user_id = (callback_query.get("from") or {}).get("id")
    data = callback_query.get("data") or ""

    if not chat_id or not user_id:
        return

    reply_markup = None

    try:
        dispatched = dispatch_callback(data, str(user_id), config)
        if dispatched is None:
            return
        reply, reply_markup = dispatched
    except UserInputError as error:
        reply = str(error)
    except AiGenerationError:
        reply = "AI could not generate this card. Please try again later."
    except Exception as error:
        log_event("callback_error", error_type=type(error).__name__)
        reply = GENERIC_ERROR_TEXT

    send_telegram_message(chat_id=chat_id, text=reply, config=config, reply_markup=reply_markup)


def handle_telegram_webhook(event: dict[str, Any]) -> dict[str, Any]:
    config = get_app_config()
    require_secret_header(event, "x-telegram-bot-api-secret-token", config["TELEGRAM_WEBHOOK_SECRET"])

    update = parse_json_body(event)

    callback_query = update.get("callback_query")
    if isinstance(callback_query, dict):
        handle_callback_query(callback_query, config)
        return json_response(200, {"ok": True})

    message = extract_telegram_message(update)

    if not message:
        return json_response(200, {"ok": True, "ignored": True})

    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text")

    if not chat_id or not isinstance(text, str) or not text.strip():
        return json_response(200, {"ok": True, "ignored": True})

    telegram_user_id = str((message.get("from") or {}).get("id") or chat_id)
    reply_markup = None

    try:
        reply, reply_markup = process_telegram_text(
            text, config, telegram_user_id=telegram_user_id
        )
    except UserInputError as error:
        # Input-format errors quote HELP_TEXT, so attach the help buttons too.
        reply, reply_markup = str(error), build_help_menu()
    except AiGenerationError:
        reply = "AI could not generate this card. Please try again later."
    except Exception as error:
        log_event("telegram_processing_error", error_type=type(error).__name__)
        reply = GENERIC_ERROR_TEXT

    send_telegram_message(chat_id=chat_id, text=reply, config=config, reply_markup=reply_markup)

    # Always 200 so Telegram does not retry handled updates.
    return json_response(200, {"ok": True})


# ---------------------------------------------------------------------------
# Lambda handler
# ---------------------------------------------------------------------------


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    log_safe_request(event)

    method = (event.get("requestContext", {}).get("http", {}).get("method") or "").upper()
    path = event.get("rawPath") or event.get("path") or "/"

    try:
        if method == "GET" and path in {"/", "/health"}:
            return handle_health()

        if method == "POST" and path == "/add-card":
            return handle_add_card(event)

        if method == "POST" and path == "/telegram":
            return handle_telegram_webhook(event)

        return json_response(404, {"ok": False, "error": "Not found"})

    except HttpError as error:
        return json_response(error.status_code, {"ok": False, "error": error.message})

    except requests.Timeout:
        log_event("request_timeout")
        return json_response(502, {"ok": False, "error": "External request timed out"})

    except requests.RequestException as error:
        log_event("request_exception", error_type=type(error).__name__)
        return json_response(502, {"ok": False, "error": "External request failed"})

    except Exception as error:
        log_event("unexpected_error", error_type=type(error).__name__)
        return json_response(500, {"ok": False, "error": "Internal Server Error"})
