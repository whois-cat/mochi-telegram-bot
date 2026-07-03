from __future__ import annotations

import base64
import hmac
import html
import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import boto3
import requests
from botocore.exceptions import ClientError

from app.config import (
    GEMINI_TIMEOUT_MS,
    GENERIC_ERROR_TEXT,
    HELP_TEXT,
    MOCHI_API_URL,
    MOCHI_CARD_TEMPLATE,
    MOCHI_TIMEOUT_SECONDS,
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_RESERVED,
    TELEGRAM_API_BASE,
    TELEGRAM_TIMEOUT_SECONDS,
    USAGE_ALIASES,
    get_app_config,
    get_known_words_table_name,
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
    mode: str  # "manual" | "ai" | "help"
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
        "usage_tag": usage_tag,
        "source": source,
        "status": STATUS_RESERVED,
        "created_at": now,
        "updated_at": now,
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
        )

    word_key = reservation["item"]["word_key"]

    try:
        mochi_card = create_mochi_card(card, usage_tag, config)
    except Exception as error:
        mark_known_word_failed(word_key=word_key, error_message=str(error))
        raise

    mochi_card_id = mochi_card.get("id")
    mark_known_word_created(word_key=word_key, mochi_card_id=mochi_card_id)

    return AddCardResult(created=True, word=card.word, mochi_card_id=mochi_card_id)


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
        )

    card = generate_card_with_ai(word_or_phrase, config)
    return add_card(card, config, source="telegram")


# ---------------------------------------------------------------------------
# Telegram helpers
# ---------------------------------------------------------------------------


def send_telegram_message(*, chat_id: int | str, text: str, config: dict[str, str]) -> None:
    response = requests.post(
        f"{TELEGRAM_API_BASE}/bot{config['TELEGRAM_BOT_TOKEN']}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
        timeout=TELEGRAM_TIMEOUT_SECONDS,
    )

    if response.status_code >= 400:
        log_event(
            "telegram_send_message_error",
            status_code=response.status_code,
            response_preview=response.text[:300],
        )


def parse_telegram_command(text: str) -> TelegramCommand:
    """
    Supported:
      /add word | translation | usage | example  -> mode="manual"
      /ai word_or_phrase                         -> mode="ai"
      /help, /start                              -> mode="help"
    Anything else raises UserInputError with the format help.
    """
    text = text.strip()
    command, _, rest = text.partition(" ")
    command = command.split("@", 1)[0].lower()  # supports /add@YourBotName
    rest = rest.strip()

    if command in ("/help", "/start"):
        return TelegramCommand(mode="help")

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


def build_add_result_reply(result: dict[str, Any]) -> str:
    word = html.escape(str(result.get("word", "")))

    if result.get("created"):
        return f"✅ Added: <b>{word}</b>"

    return f"🔁 Exists: <b>{word}</b>"


def process_telegram_text(text: str, config: dict[str, str]) -> str:
    command = parse_telegram_command(text)
    log_event("telegram_command", mode=command.mode)

    if command.mode == "help":
        return HELP_TEXT

    if command.mode == "manual":
        return build_add_result_reply(add_card(command.card, config, source="telegram").to_dict())

    return build_add_result_reply(add_ai_card(command.query, config).to_dict())


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


def handle_telegram_webhook(event: dict[str, Any]) -> dict[str, Any]:
    config = get_app_config()
    require_secret_header(event, "x-telegram-bot-api-secret-token", config["TELEGRAM_WEBHOOK_SECRET"])

    update = parse_json_body(event)
    message = extract_telegram_message(update)

    if not message:
        return json_response(200, {"ok": True, "ignored": True})

    chat_id = (message.get("chat") or {}).get("id")
    text = message.get("text")

    if not chat_id or not isinstance(text, str) or not text.strip():
        return json_response(200, {"ok": True, "ignored": True})

    try:
        reply = process_telegram_text(text, config)
    except UserInputError as error:
        reply = str(error)
    except AiGenerationError:
        reply = "AI could not generate this card. Please try again later."
    except Exception as error:
        log_event("telegram_processing_error", error_type=type(error).__name__)
        reply = GENERIC_ERROR_TEXT

    send_telegram_message(chat_id=chat_id, text=reply, config=config)

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
