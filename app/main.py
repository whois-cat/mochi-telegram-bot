from __future__ import annotations

import base64
import hmac
import html
import json
import re
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any

import boto3
import requests
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

from app.keyboards import build_help_menu, build_inline_keyboard
from app.config import (
    ADDED_LINE,
    AI_COMMAND_DESTINATIONS,
    AI_ERROR_TEXT,
    BULK_AI_LIST_MARKER_PATTERN,
    BULK_AI_MAX_WORDS,
    BULK_AI_MAX_WORKERS,
    BULK_AI_SEPARATORS_PATTERN,
    BULK_AI_TOO_MANY_TEXT,
    CALLBACK_CANCEL,
    CALLBACK_DELETE_CONFIRM_PREFIX,
    CALLBACK_DELETE_PREFIX,
    CALLBACK_EDIT_PREFIX,
    CALLBACK_INBOX_PROMOTE,
    CALLBACK_REGEN_PREFIX,
    CALLBACK_TO_INBOX_PREFIX,
    CALLBACK_TO_MOCHI_PREFIX,
    CANCELLED_TEXT,
    CANCEL_BUTTON_TEXT,
    CEFR_LEVELS,
    CONFIRM_DELETE_BUTTON_TEXT,
    DELETE_BUTTON_TEXT,
    DESTINATION_INBOX,
    DESTINATION_LABELS,
    DESTINATION_MOCHI,
    EDIT_BUTTON_TEXT,
    EDIT_CARD_PROMPT,
    EDIT_SESSION_TTL_SECONDS,
    FAILED_LINE,
    FREQUENCY_VALUES,
    GEMINI_TIMEOUT_MS,
    GENERIC_ERROR_TEXT,
    HELP_TEXT,
    INBOX_EMPTY_TEXT,
    INBOX_LIST_LIMIT,
    INBOX_NOTHING_TO_PROMOTE_TEXT,
    INBOX_PROMOTE_BATCH,
    INBOX_PROMOTE_BUTTON_TEXT,
    INBOX_PROMOTE_MIN_SEEN,
    INBOX_SUGGEST_SEEN_COUNT,
    INBOX_SUGGEST_TEXT,
    INBOX_WORD_TO_MOCHI_BUTTON_TEXT,
    LEGACY_CARD_SRS_ATTRIBUTES,
    MOCHI_API_URL,
    MOCHI_CARD_TEMPLATE,
    MOCHI_TIMEOUT_SECONDS,
    REGENERATE_BUTTON_TEXT,
    SESSION_STATUS_ACTIVE,
    SEEN_COUNT_TEXT,
    SESSION_TYPE_EDIT,
    STATUS_CREATED,
    STATUS_DRAFT,
    STATUS_FAILED,
    STATUS_INBOX,
    STATUS_RESERVED,
    TELEGRAM_API_BASE,
    TELEGRAM_BOT_COMMANDS,
    TELEGRAM_CALLBACK_DATA_MAX_BYTES,
    TELEGRAM_TIMEOUT_SECONDS,
    TO_INBOX_BUTTON_TEXT,
    TO_MOCHI_BUTTON_TEXT,
    USAGE_ALIASES,
    WORD_NOT_FOUND_TEXT,
    WORD_STATUS_LABELS,
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
    frequency: str = ""
    level: str = ""


@dataclass(frozen=True)
class AddCardResult:
    # created: this request created the word or moved it to a new status.
    created: bool
    word: str
    status: str = STATUS_CREATED
    mochi_card_id: str | None = None
    translation: str | None = None
    example: str | None = None
    frequency: str = ""
    level: str = ""
    seen_count: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": True,
            "created": self.created,
            "word": self.word,
            "status": self.status,
            "mochi_card_id": self.mochi_card_id,
            "message": "Card added to Mochi" if self.created else "Card already exists",
        }


@dataclass(frozen=True)
class TelegramCommand:
    mode: str  # "manual" | "ai" | "inbox_list" | "delete" | "help" | "cancel"
    card: CardInput | None = None
    query: str | None = None
    queries: tuple[str, ...] = ()
    destination: str | None = None  # DESTINATION_MOCHI | DESTINATION_INBOX | None (draft)


# One Telegram message: (html_text, reply_markup or None).
Reply = tuple[str, "dict[str, Any] | None"]


# ---------------------------------------------------------------------------
# Safe logging
# ---------------------------------------------------------------------------


def log_event(message: str, **fields: Any) -> None:
    """Structured log line. Never pass secrets, headers, bodies or payloads."""
    print(json.dumps({"message": message, **fields}, ensure_ascii=False))


def log_gemini_request_error(error: Exception) -> None:
    # code/status exist only on google.genai APIError, not on timeouts/network errors.
    log_event(
        "gemini_request_error",
        error_type=type(error).__name__,
        code=getattr(error, "code", None),
        status=getattr(error, "status", None),
    )


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


def remove_expression_for(attributes: tuple[str, ...]) -> str:
    return " REMOVE " + ", ".join(attributes)


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


def put_new_known_word(
    *,
    card: CardInput,
    usage_tag: str,
    status: str,
    source: str,
    config: dict[str, str],
) -> dict[str, Any] | None:
    """
    Store a new word as DRAFT or INBOX. Succeeds when the item does not exist or
    its previous Mochi attempt FAILED (retry). None when the word already exists.
    """
    normalized_word = normalize_word_for_lookup(card.word)
    now = utc_now_iso()

    item = {
        "word_key": build_word_key(deck_id=config["MOCHI_DECK_ID"], normalized_word=normalized_word),
        "deck_id": config["MOCHI_DECK_ID"],
        "normalized_word": normalized_word,
        "word": card.word,
        "translation": card.translation,
        "example": card.example,
        "usage_tag": usage_tag,
        "frequency": card.frequency,
        "level": card.level,
        "source": source,
        "status": status,
        "seen_count": 1,
        "created_at": now,
        "updated_at": now,
        "last_seen_at": now,
    }

    try:
        get_known_words_table().put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(word_key) OR #status = :failed",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":failed": STATUS_FAILED},
        )
        return item

    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return None


def record_sighting(word_key: str) -> dict[str, Any]:
    """The word was added again while still a draft or in inbox: count it."""
    now = utc_now_iso()
    return get_known_words_table().update_item(
        Key={"word_key": word_key},
        UpdateExpression="SET last_seen_at = :now, updated_at = :now ADD seen_count :one",
        ExpressionAttributeValues={":now": now, ":one": 1},
        ReturnValues="ALL_NEW",
    )["Attributes"]


def set_known_word_status(
    word_key: str,
    *,
    status: str,
    from_statuses: tuple[str, ...],
) -> dict[str, Any] | None:
    """Atomic status change; None when the word is missing or not in from_statuses."""
    placeholders = {f":from{index}": value for index, value in enumerate(from_statuses)}

    try:
        return get_known_words_table().update_item(
            Key={"word_key": word_key},
            UpdateExpression="SET #status = :status, updated_at = :updated_at",
            ConditionExpression=f"#status IN ({', '.join(placeholders)})",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":status": status, ":updated_at": utc_now_iso(), **placeholders},
            ReturnValues="ALL_NEW",
        )["Attributes"]

    except ClientError as error:
        if error.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        return None


def scan_inbox_items(config: dict[str, str]) -> list[dict[str, Any]]:
    """Most-seen first, then oldest. Scan is fine here: small personal table."""
    table = get_known_words_table()
    scan_kwargs: dict[str, Any] = {
        "FilterExpression": Attr("status").eq(STATUS_INBOX) & Attr("deck_id").eq(config["MOCHI_DECK_ID"]),
    }
    items: list[dict[str, Any]] = []

    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    items.sort(key=lambda item: (-int(item.get("seen_count") or 1), str(item.get("created_at") or "")))
    return items


def card_from_item(item: dict[str, Any]) -> CardInput:
    return CardInput(
        word=str(item.get("word") or ""),
        translation=str(item.get("translation") or ""),
        usage=str(item.get("usage_tag") or ""),
        example=str(item.get("example") or ""),
        frequency=str(item.get("frequency") or ""),
        level=str(item.get("level") or ""),
    )


def result_from_item(item: dict[str, Any], *, created: bool) -> AddCardResult:
    return AddCardResult(
        created=created,
        word=str(item.get("word") or ""),
        status=str(item.get("status") or STATUS_CREATED),
        mochi_card_id=str(item.get("mochi_card_id") or "") or None,
        translation=str(item.get("translation") or "") or None,
        example=str(item.get("example") or "") or None,
        frequency=str(item.get("frequency") or ""),
        level=str(item.get("level") or ""),
        seen_count=int(item.get("seen_count") or 1),
    )


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


def build_mochi_tags(card: CardInput, usage_tag: str) -> list[str]:
    tags = ["english", "from-telegram", usage_tag]
    if card.frequency:
        tags.append(f"freq-{card.frequency}")
    if card.level:
        tags.append(card.level.lower())
    return tags


def create_mochi_card(card: CardInput, usage_tag: str, config: dict[str, str]) -> dict[str, Any]:
    payload = {
        "deck-id": config["MOCHI_DECK_ID"],
        "content": build_mochi_content(card),
        "manual-tags": build_mochi_tags(card, usage_tag),
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
) -> bool:
    """False when the card no longer exists in Mochi (deleted there manually)."""
    payload = {
        "content": build_mochi_content(card),
        "manual-tags": build_mochi_tags(card, usage_tag),
    }

    response = requests.post(
        f"{MOCHI_API_URL}{card_id}",
        json=payload,
        auth=(config["MOCHI_API_KEY"], ""),
        headers={"Accept": "application/json", "Content-Type": "application/json"},
        timeout=MOCHI_TIMEOUT_SECONDS,
    )

    if response.status_code == 404:
        return False

    if response.status_code >= 400:
        log_event(
            "mochi_api_error",
            status_code=response.status_code,
            response_preview=response.text[:300],
        )
        raise HttpError(502, "Mochi API request failed")

    return True


def delete_mochi_card(card_id: str, config: dict[str, str]) -> None:
    """404 is fine: the card is already gone in Mochi."""
    response = requests.delete(
        f"{MOCHI_API_URL}{card_id}",
        auth=(config["MOCHI_API_KEY"], ""),
        headers={"Accept": "application/json"},
        timeout=MOCHI_TIMEOUT_SECONDS,
    )

    if response.status_code >= 400 and response.status_code != 404:
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
        log_gemini_request_error(error)
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

    # Optional estimates: an unexpected value is dropped, never fails the card.
    frequency = str(data.get("frequency") or "").strip().lower()
    level = str(data.get("level") or "").strip().upper()

    return CardInput(
        **fields,
        frequency=frequency if frequency in FREQUENCY_VALUES else "",
        level=level if level in CEFR_LEVELS else "",
    )


# ---------------------------------------------------------------------------
# Central add-card flow
# ---------------------------------------------------------------------------


def send_known_word_to_mochi(word_key: str, config: dict[str, str]) -> AddCardResult:
    """DRAFT / INBOX / FAILED -> Mochi. A word already in Mochi is returned as is."""
    item = set_known_word_status(
        word_key,
        status=STATUS_RESERVED,
        from_statuses=(STATUS_DRAFT, STATUS_INBOX, STATUS_FAILED),
    )

    if item is None:
        existing = get_known_words_table().get_item(Key={"word_key": word_key}, ConsistentRead=True).get("Item")
        if not existing:
            raise UserInputError(WORD_NOT_FOUND_TEXT)
        return result_from_item(existing, created=False)

    card = card_from_item(item)

    try:
        mochi_card = create_mochi_card(card, normalize_usage_tag(card.usage), config)
    except Exception as error:
        mark_known_word_failed(word_key=word_key, error_message=str(error))
        raise

    mochi_card_id = mochi_card.get("id")
    mark_known_word_created(word_key=word_key, mochi_card_id=mochi_card_id)
    log_event("word_sent_to_mochi", previous_seen_count=int(item.get("seen_count") or 1))

    return result_from_item({**item, "status": STATUS_CREATED, "mochi_card_id": mochi_card_id}, created=True)


def move_known_word_to_inbox(word_key: str) -> AddCardResult:
    item = set_known_word_status(word_key, status=STATUS_INBOX, from_statuses=(STATUS_DRAFT, STATUS_FAILED))

    if item is None:
        existing = get_known_words_table().get_item(Key={"word_key": word_key}, ConsistentRead=True).get("Item")
        if not existing:
            raise UserInputError(WORD_NOT_FOUND_TEXT)
        return result_from_item(existing, created=False)

    return result_from_item(item, created=True)


def handle_existing_word(
    item: dict[str, Any],
    destination: str | None,
    config: dict[str, str],
) -> AddCardResult:
    """Draft/inbox words count one more sighting and then go where asked."""
    if item.get("status") not in (STATUS_DRAFT, STATUS_INBOX):
        return result_from_item(item, created=False)

    item = record_sighting(item["word_key"])

    if destination == DESTINATION_MOCHI:
        return send_known_word_to_mochi(item["word_key"], config)

    if destination == DESTINATION_INBOX:
        return move_known_word_to_inbox(item["word_key"])

    return result_from_item(item, created=False)


def store_new_card(
    card: CardInput,
    config: dict[str, str],
    *,
    destination: str | None,
    source: str,
) -> AddCardResult:
    item = put_new_known_word(
        card=card,
        usage_tag=normalize_usage_tag(card.usage),
        status=STATUS_INBOX if destination == DESTINATION_INBOX else STATUS_DRAFT,
        source=source,
        config=config,
    )

    if item is None:  # the same word was added concurrently
        existing = get_known_word(card.word, config)
        if not existing:
            raise UserInputError(WORD_NOT_FOUND_TEXT)
        return handle_existing_word(existing, destination, config)

    if destination == DESTINATION_MOCHI:
        return send_known_word_to_mochi(item["word_key"], config)

    return result_from_item(item, created=True)


def add_card(
    card: CardInput,
    config: dict[str, str],
    *,
    destination: str | None,
    source: str = "telegram",
) -> AddCardResult:
    """
    Single flow used by POST /add-card and Telegram /add: an existing word is
    reused (and counted), a new one is stored as a draft, in inbox, or sent to Mochi.
    """
    existing = get_known_word(card.word, config)

    if existing and existing.get("status") != STATUS_FAILED:
        return handle_existing_word(existing, destination, config)

    return store_new_card(card, config, destination=destination, source=source)


def split_bulk_words(text: str) -> list[str]:
    """One word/phrase per line or ";"-separated; list markers stripped, duplicates dropped."""
    words: list[str] = []
    seen: set[str] = set()

    for chunk in re.split(BULK_AI_SEPARATORS_PATTERN, text):
        word = re.sub(BULK_AI_LIST_MARKER_PATTERN, "", chunk).strip()
        lookup = normalize_word_for_lookup(word)
        if lookup and lookup not in seen:
            seen.add(lookup)
            words.append(word)

    return words


def generate_card_or_none(word_or_phrase: str, config: dict[str, str]) -> CardInput | None:
    try:
        return generate_card_with_ai(word_or_phrase, config)
    except AiGenerationError:
        return None


def result_or_none(action: Callable[[], AddCardResult]) -> AddCardResult | None:
    """One failed word must not stop the rest of a list."""
    try:
        return action()
    except Exception as error:
        log_event("add_word_error", error_type=type(error).__name__)
        return None


def add_ai_cards(
    words: list[str],
    config: dict[str, str],
    *,
    destination: str | None,
) -> list[tuple[str, AddCardResult | None]]:
    """
    /ai, /mochi and /inbox for one or many words. Existing words never spend a
    Gemini call; Gemini runs in parallel, DynamoDB and Mochi writes stay
    sequential. None means the word failed.
    """
    results: dict[str, AddCardResult | None] = {}
    to_generate: list[str] = []

    for word in words:
        existing = get_known_word(word, config)
        if existing and existing.get("status") != STATUS_FAILED:
            results[word] = result_or_none(lambda: handle_existing_word(existing, destination, config))
        else:
            to_generate.append(word)

    if to_generate:
        with ThreadPoolExecutor(max_workers=min(BULK_AI_MAX_WORKERS, len(to_generate))) as pool:
            cards = list(pool.map(lambda word: generate_card_or_none(word, config), to_generate))

        for word, card in zip(to_generate, cards):
            if card is None:
                results[word] = None
                continue
            results[word] = result_or_none(
                lambda: store_new_card(card, config, destination=destination, source="telegram")
            )

    return [(word, results[word]) for word in words]


def promote_seen_inbox_words(config: dict[str, str]) -> list[tuple[str, AddCardResult | None]]:
    """Inbox words seen INBOX_PROMOTE_MIN_SEEN+ times -> Mochi, a batch per call."""
    candidates = [
        item
        for item in scan_inbox_items(config)
        if int(item.get("seen_count") or 1) >= INBOX_PROMOTE_MIN_SEEN
    ][:INBOX_PROMOTE_BATCH]

    return [
        (
            str(item.get("word") or ""),
            result_or_none(lambda: send_known_word_to_mochi(item["word_key"], config)),
        )
        for item in candidates
    ]


def regenerate_card(normalized_word: str, config: dict[str, str]) -> AddCardResult:
    """
    Regenerate translation/example with Gemini for an already saved word.
    Words in Mochi are synced there too; drafts and inbox words stay local.
    """
    table = get_known_words_table()
    word_key = build_word_key(
        deck_id=config["MOCHI_DECK_ID"],
        normalized_word=normalized_word,
    )
    item = table.get_item(Key={"word_key": word_key}, ConsistentRead=True).get("Item")

    if not item:
        raise UserInputError(WORD_NOT_FOUND_TEXT)

    card = generate_card_with_ai(str(item.get("word") or normalized_word), config)
    usage_tag = normalize_usage_tag(card.usage)

    mochi_card_id = str(item.get("mochi_card_id") or "")
    if item.get("status") == STATUS_CREATED:
        mochi_card_id = sync_mochi_card_for_known_word(item, card, usage_tag, config)

    update_expression = (
        "SET #translation = :translation, example = :example, "
        "usage_tag = :usage_tag, frequency = :frequency, #level = :level, "
        "mochi_card_id = :mochi_card_id, updated_at = :updated_at"
    )
    update_expression += remove_expression_for(LEGACY_CARD_SRS_ATTRIBUTES)

    updated = table.update_item(
        Key={"word_key": word_key},
        # "translation" and "level" are DynamoDB reserved keywords.
        UpdateExpression=update_expression,
        ExpressionAttributeNames={"#translation": "translation", "#level": "level"},
        ExpressionAttributeValues={
            ":translation": card.translation,
            ":example": card.example,
            ":usage_tag": usage_tag,
            ":frequency": card.frequency,
            ":level": card.level,
            ":mochi_card_id": mochi_card_id,
            ":updated_at": utc_now_iso(),
        },
        ReturnValues="ALL_NEW",
    )["Attributes"]

    log_event("card_regenerated", has_mochi_card=bool(mochi_card_id))
    return result_from_item(updated, created=False)


def sync_mochi_card_for_known_word(
    existing_item: dict[str, Any],
    card: CardInput,
    usage_tag: str,
    config: dict[str, str],
) -> str:
    mochi_card_id = str(existing_item.get("mochi_card_id") or "")

    if mochi_card_id and update_mochi_card(mochi_card_id, card, usage_tag, config):
        return mochi_card_id

    return str(create_mochi_card(card, usage_tag, config).get("id") or "")


def build_known_word_item_from_card(
    *,
    existing_item: dict[str, Any],
    card: CardInput,
    usage_tag: str,
    mochi_card_id: str,
    config: dict[str, str],
) -> dict[str, Any]:
    now = utc_now_iso()
    normalized_word = normalize_word_for_lookup(card.word)
    item = {
        **existing_item,
        "word_key": build_word_key(
            deck_id=config["MOCHI_DECK_ID"],
            normalized_word=normalized_word,
        ),
        "deck_id": config["MOCHI_DECK_ID"],
        "normalized_word": normalized_word,
        "word": card.word,
        "translation": card.translation,
        "example": card.example,
        "usage_tag": usage_tag,
        "mochi_card_id": mochi_card_id,
        "updated_at": now,
    }
    item.setdefault("created_at", now)

    for old_srs_attr in LEGACY_CARD_SRS_ATTRIBUTES:
        item.pop(old_srs_attr, None)

    return item


def update_known_word_from_edit(
    *,
    old_word_key: str,
    card: CardInput,
    config: dict[str, str],
) -> AddCardResult:
    table = get_known_words_table()
    existing_item = table.get_item(Key={"word_key": old_word_key}, ConsistentRead=True).get("Item")

    if not existing_item:
        raise UserInputError(WORD_NOT_FOUND_TEXT)

    # The edit format has no frequency/level; keep the stored estimates.
    card = replace(
        card,
        frequency=str(existing_item.get("frequency") or ""),
        level=str(existing_item.get("level") or ""),
    )
    usage_tag = normalize_usage_tag(card.usage)
    new_word_key = word_key_for(card.word, config)

    if new_word_key != old_word_key:
        collision = table.get_item(Key={"word_key": new_word_key}, ConsistentRead=True).get("Item")
        if collision:
            raise UserInputError("A card with this word already exists.")

    mochi_card_id = str(existing_item.get("mochi_card_id") or "")
    if existing_item.get("status") == STATUS_CREATED:
        mochi_card_id = sync_mochi_card_for_known_word(existing_item, card, usage_tag, config)

    item = build_known_word_item_from_card(
        existing_item=existing_item,
        card=card,
        usage_tag=usage_tag,
        mochi_card_id=mochi_card_id,
        config=config,
    )

    if new_word_key != old_word_key:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(word_key)",
        )
        table.delete_item(Key={"word_key": old_word_key})
    else:
        expression_values: dict[str, Any] = {
            ":normalized_word": item["normalized_word"],
            ":word": item["word"],
            ":translation": item["translation"],
            ":example": item["example"],
            ":usage_tag": item["usage_tag"],
            ":mochi_card_id": item["mochi_card_id"],
            ":status": item["status"],
            ":updated_at": item["updated_at"],
        }
        update_expression = (
            "SET normalized_word = :normalized_word, #word = :word, "
            "#translation = :translation, example = :example, usage_tag = :usage_tag, "
            "mochi_card_id = :mochi_card_id, #status = :status, updated_at = :updated_at"
        )

        update_expression += remove_expression_for(LEGACY_CARD_SRS_ATTRIBUTES)

        table.update_item(
            Key={"word_key": old_word_key},
            UpdateExpression=update_expression,
            ExpressionAttributeNames={
                "#translation": "translation",
                "#status": "status",
                "#word": "word",
            },
            ExpressionAttributeValues=expression_values,
        )

    log_event("card_edited", word_changed=new_word_key != old_word_key, has_mochi_card=bool(mochi_card_id))
    return result_from_item(item, created=False)


def delete_known_word(normalized_word: str, config: dict[str, str]) -> str:
    """Delete the word from DynamoDB and its card from Mochi."""
    table = get_known_words_table()
    word_key = build_word_key(
        deck_id=config["MOCHI_DECK_ID"],
        normalized_word=normalized_word,
    )
    item = table.get_item(Key={"word_key": word_key}, ConsistentRead=True).get("Item")

    if not item:
        return WORD_NOT_FOUND_TEXT

    mochi_card_id = item.get("mochi_card_id")
    if mochi_card_id:
        delete_mochi_card(str(mochi_card_id), config)

    table.delete_item(Key={"word_key": word_key})
    log_event("card_deleted", has_mochi_card=bool(mochi_card_id))

    word = html.escape(str(item.get("word") or normalized_word))
    return f"Deleted: <b>{word}</b>"


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


def send_telegram_replies(*, chat_id: int | str, replies: list[Reply], config: dict[str, str]) -> None:
    for text, reply_markup in replies:
        send_telegram_message(chat_id=chat_id, text=text, config=config, reply_markup=reply_markup)


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


def sync_telegram_bot_commands(config: dict[str, str]) -> dict[str, Any]:
    response = requests.post(
        f"{TELEGRAM_API_BASE}/bot{config['TELEGRAM_BOT_TOKEN']}/setMyCommands",
        json={"commands": list(TELEGRAM_BOT_COMMANDS)},
        timeout=TELEGRAM_TIMEOUT_SECONDS,
    )

    if response.status_code >= 400:
        log_event(
            "telegram_sync_commands_error",
            status_code=response.status_code,
            response_preview=response.text[:300],
        )
        raise HttpError(502, "Telegram command sync failed")

    return {
        "ok": True,
        "commands": [command["command"] for command in TELEGRAM_BOT_COMMANDS],
    }


def callback_data_for(prefix: str, word: str) -> str | None:
    """None when the word does not fit Telegram's callback_data size limit."""
    data = prefix + normalize_word_for_lookup(word)

    if len(data.encode("utf-8")) > TELEGRAM_CALLBACK_DATA_MAX_BYTES:
        return None

    return data


def build_card_actions_keyboard(word: str, status: str) -> dict[str, Any] | None:
    """Drafts get To Mochi + Inbox, inbox words get To Mochi; everyone gets Regenerate/Edit/Delete."""
    to_mochi = callback_data_for(CALLBACK_TO_MOCHI_PREFIX, word)
    to_inbox = callback_data_for(CALLBACK_TO_INBOX_PREFIX, word)
    regen = callback_data_for(CALLBACK_REGEN_PREFIX, word)
    edit = callback_data_for(CALLBACK_EDIT_PREFIX, word)
    delete = callback_data_for(CALLBACK_DELETE_PREFIX, word)

    if not all((to_mochi, to_inbox, regen, edit, delete)):
        return None

    rows = []
    if status in (STATUS_DRAFT, STATUS_FAILED):
        rows.append(
            [
                {"text": TO_MOCHI_BUTTON_TEXT, "callback_data": to_mochi},
                {"text": TO_INBOX_BUTTON_TEXT, "callback_data": to_inbox},
            ]
        )
    elif status == STATUS_INBOX:
        rows.append([{"text": TO_MOCHI_BUTTON_TEXT, "callback_data": to_mochi}])

    rows.append(
        [
            {"text": REGENERATE_BUTTON_TEXT, "callback_data": regen},
            {"text": EDIT_BUTTON_TEXT, "callback_data": edit},
            {"text": DELETE_BUTTON_TEXT, "callback_data": delete},
        ]
    )
    return {"inline_keyboard": rows}


def build_delete_confirmation(normalized_word: str, config: dict[str, str]) -> tuple[str, dict[str, Any] | None]:
    word_key = build_word_key(
        deck_id=config["MOCHI_DECK_ID"],
        normalized_word=normalized_word,
    )
    item = get_known_words_table().get_item(Key={"word_key": word_key}).get("Item")

    if not item:
        return WORD_NOT_FOUND_TEXT, None

    word = str(item.get("word") or normalized_word)
    confirm = callback_data_for(CALLBACK_DELETE_CONFIRM_PREFIX, word)

    if not confirm:
        return WORD_NOT_FOUND_TEXT, None

    keyboard = {
        "inline_keyboard": [
            [
                {"text": CONFIRM_DELETE_BUTTON_TEXT, "callback_data": confirm},
                {"text": CANCEL_BUTTON_TEXT, "callback_data": CALLBACK_CANCEL},
            ]
        ]
    }
    question = f"Delete <b>{html.escape(word)}</b>?"
    if item.get("mochi_card_id"):
        question += " It will also be removed from Mochi."
    return question, keyboard


def parse_telegram_command(text: str) -> TelegramCommand:
    """
    Supported:
      /add word | translation | usage | example  -> mode="manual"
      /ai | /mochi | /inbox word_or_phrase       -> mode="ai" (draft | Mochi | inbox)
        one word per line or ";"-separated for several words
      /inbox                                     -> mode="inbox_list"
      /delete word                               -> mode="delete"
      /help, /start                              -> mode="help"
      /cancel                                    -> mode="cancel"
    Anything else raises UserInputError with the format help.
    """
    text = text.strip()
    # Split on any whitespace so "/ai\nword1\nword2" keeps the newlines in rest.
    command, rest = (text.split(maxsplit=1) + [""])[:2]
    command = command.split("@", 1)[0].lower()  # supports /add@YourBotName
    rest = rest.strip()

    if command in ("/help", "/start"):
        return TelegramCommand(mode="help")

    if command == "/cancel":
        return TelegramCommand(mode="cancel")

    if command == "/add":
        parts = [part.strip() for part in rest.split("|")]
        if len(parts) != 4 or any(not part for part in parts):
            raise UserInputError(f"Please use this format:\n\n{HELP_TEXT}")

        word, translation, usage, example = parts
        return TelegramCommand(
            mode="manual",
            card=CardInput(word=word, translation=translation, usage=usage, example=example),
        )

    if command == "/inbox" and not rest:
        return TelegramCommand(mode="inbox_list")

    if command in AI_COMMAND_DESTINATIONS:
        words = split_bulk_words(rest)
        if not words:
            raise UserInputError(f"Please add a word or phrase:\n\n{HELP_TEXT}")
        if len(words) > BULK_AI_MAX_WORDS:
            raise UserInputError(BULK_AI_TOO_MANY_TEXT.format(limit=BULK_AI_MAX_WORDS, count=len(words)))
        return TelegramCommand(mode="ai", queries=tuple(words), destination=AI_COMMAND_DESTINATIONS[command])

    if command == "/delete":
        if not rest:
            raise UserInputError(f"Please add a word to delete:\n\n{HELP_TEXT}")
        return TelegramCommand(mode="delete", query=rest)

    raise UserInputError(f"I don't know that command.\n\n{HELP_TEXT}")


def extract_telegram_message(update: dict[str, Any]) -> dict[str, Any] | None:
    for key in ("message", "edited_message"):
        message = update.get(key)
        if isinstance(message, dict):
            return message
    return None


def format_word_meta(*, frequency: str, level: str, status: str, seen_count: int) -> str:
    """E.g. "common · B2 · in inbox · looked up 3 times"; empty when there is nothing to show."""
    parts = [part for part in (frequency, level, WORD_STATUS_LABELS.get(status, "")) if part]
    if seen_count > 1:
        parts.append(SEEN_COUNT_TEXT.format(count=seen_count))
    return " · ".join(parts)


def is_just_added(result: AddCardResult) -> bool:
    """Just sent to Mochi or Inbox: a one-line confirmation is enough."""
    return result.created and result.status in DESTINATION_LABELS


def build_added_line(result: AddCardResult) -> str:
    return ADDED_LINE.format(word=html.escape(result.word), destination=DESTINATION_LABELS[result.status])


def build_failed_line(word: str) -> str:
    return FAILED_LINE.format(word=html.escape(word))


def build_add_result_reply(result: AddCardResult) -> Reply:
    """A confirmation line, or the card with buttons that depend on where the word is."""
    if is_just_added(result):
        return build_added_line(result), None

    # Word, translation below it, the example as a quote, meta as an italic caption.
    lines = [f"<b>{html.escape(result.word)}</b>"]
    if result.translation:
        lines.append(html.escape(result.translation))
    if result.example:
        lines.append(f"<blockquote>{html.escape(result.example)}</blockquote>")

    meta = format_word_meta(
        frequency=result.frequency,
        level=result.level,
        status=result.status,
        seen_count=result.seen_count,
    )
    if meta:
        lines.append(f"<i>{html.escape(meta)}</i>")
    if result.status in (STATUS_DRAFT, STATUS_INBOX) and result.seen_count >= INBOX_SUGGEST_SEEN_COUNT:
        lines.append(INBOX_SUGGEST_TEXT)

    return "\n".join(lines), build_card_actions_keyboard(result.word, result.status)


def build_word_replies(results: list[tuple[str, AddCardResult | None]]) -> list[Reply]:
    """Confirmations and failures share one message; cards that need a choice get their own."""
    lines: list[str] = []
    cards: list[Reply] = []

    for word, result in results:
        if result is None:
            lines.append(build_failed_line(word))
        elif is_just_added(result):
            lines.append(build_added_line(result))
        else:
            cards.append(build_add_result_reply(result))

    return ([("\n".join(lines), None)] if lines else []) + cards


def build_inbox_reply(config: dict[str, str]) -> Reply:
    items = scan_inbox_items(config)
    if not items:
        return INBOX_EMPTY_TEXT, None

    shown = items[:INBOX_LIST_LIMIT]
    lines = [f"<b>Inbox</b> · {len(items)}", ""]
    rows = []

    for index, item in enumerate(shown, start=1):
        word = str(item.get("word") or "")
        meta = format_word_meta(
            frequency=str(item.get("frequency") or ""),
            level=str(item.get("level") or ""),
            status=STATUS_DRAFT,  # every item here is in inbox; no need to say so
            seen_count=int(item.get("seen_count") or 1),
        )
        line = f"{index}. <b>{html.escape(word)}</b> — {html.escape(str(item.get('translation') or ''))}"
        if meta:
            line += f" <i>({html.escape(meta)})</i>"
        lines.append(line)

        to_mochi = callback_data_for(CALLBACK_TO_MOCHI_PREFIX, word)
        delete = callback_data_for(CALLBACK_DELETE_PREFIX, word)
        if to_mochi and delete:
            rows.append(
                [
                    {"text": INBOX_WORD_TO_MOCHI_BUTTON_TEXT.format(word=word), "callback_data": to_mochi},
                    {"text": DELETE_BUTTON_TEXT, "callback_data": delete},
                ]
            )

    if len(items) > len(shown):
        lines.append(f"… and {len(items) - len(shown)} more")

    if any(int(item.get("seen_count") or 1) >= INBOX_PROMOTE_MIN_SEEN for item in items):
        rows.append(
            [
                {
                    "text": INBOX_PROMOTE_BUTTON_TEXT.format(min_seen=INBOX_PROMOTE_MIN_SEEN),
                    "callback_data": CALLBACK_INBOX_PROMOTE,
                }
            ]
        )

    return "\n".join(lines), {"inline_keyboard": rows} if rows else None


def build_inbox_promote_replies(config: dict[str, str]) -> list[Reply]:
    results = promote_seen_inbox_words(config)
    if not results:
        return [(INBOX_NOTHING_TO_PROMOTE_TEXT.format(min_seen=INBOX_PROMOTE_MIN_SEEN), None)]

    # Fresh list so leftovers (or the next batch) can be handled right away.
    return build_word_replies(results) + [build_inbox_reply(config)]


def process_telegram_text(
    text: str,
    config: dict[str, str],
    *,
    telegram_user_id: str,
) -> list[Reply]:
    """Returns the messages to send back, usually one."""
    stripped = text.strip()

    session = get_active_session(telegram_user_id)
    if session and stripped.lower() == "/cancel":
        clear_session(telegram_user_id)
        return [(CANCELLED_TEXT, None)]

    # Plain text while an edit session is active is the edited card.
    if session and not stripped.startswith("/"):
        return [handle_edit_card_answer(session, stripped, config)]

    command = parse_telegram_command(stripped)
    log_event("telegram_command", mode=command.mode)

    if command.mode == "cancel":
        clear_session(telegram_user_id)
        return [(CANCELLED_TEXT, None)]

    if command.mode == "help":
        return [(HELP_TEXT, build_help_menu())]

    if command.mode == "delete":
        return [build_delete_confirmation(normalize_word_for_lookup(command.query), config)]

    if command.mode == "inbox_list":
        return [build_inbox_reply(config)]

    if command.mode == "manual":
        return [build_add_result_reply(add_card(command.card, config, destination=None, source="telegram"))]

    return build_word_replies(add_ai_cards(list(command.queries), config, destination=command.destination))


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def get_sessions_table():
    # The table is still named "practice-sessions" in infra; it now only holds edit sessions.
    return boto3.resource("dynamodb").Table(get_practice_sessions_table_name())


def save_session(session: dict[str, Any]) -> None:
    get_sessions_table().put_item(Item=session)


def get_active_session(telegram_user_id: str) -> dict[str, Any] | None:
    item = (
        get_sessions_table()
        .get_item(Key={"telegram_user_id": str(telegram_user_id)}, ConsistentRead=True)
        .get("Item")
    )

    # Leftover practice sessions from before practice was removed are ignored.
    if not item or item.get("status") != SESSION_STATUS_ACTIVE or item.get("session_type") != SESSION_TYPE_EDIT:
        return None

    # DynamoDB TTL deletion is lazy; treat expired sessions as gone.
    if int(item.get("expires_at") or 0) <= time.time():
        return None

    return item


def clear_session(telegram_user_id: str) -> None:
    get_sessions_table().delete_item(Key={"telegram_user_id": str(telegram_user_id)})


# ---------------------------------------------------------------------------
# Edit sessions
# ---------------------------------------------------------------------------


def parse_card_input_text(text: str) -> CardInput:
    parts = [part.strip() for part in text.split("|")]

    if len(parts) != 4 or any(not part for part in parts):
        raise UserInputError(EDIT_CARD_PROMPT)

    word, translation, usage, example = parts
    return CardInput(word=word, translation=translation, usage=usage, example=example)


def build_edit_card_prompt(item: dict[str, Any]) -> str:
    lines = [
        EDIT_CARD_PROMPT,
        "",
        "Current:",
        (
            f"{html.escape(str(item.get('word') or ''))} | "
            f"{html.escape(str(item.get('translation') or ''))} | "
            f"{html.escape(str(item.get('usage_tag') or ''))} | "
            f"{html.escape(str(item.get('example') or ''))}"
        ),
    ]
    return "\n".join(lines)


def start_edit_card_session(
    normalized_word: str,
    telegram_user_id: str,
    config: dict[str, str],
) -> tuple[str, dict[str, Any] | None]:
    word_key = build_word_key(
        deck_id=config["MOCHI_DECK_ID"],
        normalized_word=normalized_word,
    )
    item = get_known_words_table().get_item(Key={"word_key": word_key}, ConsistentRead=True).get("Item")

    if not item:
        raise UserInputError(WORD_NOT_FOUND_TEXT)

    session = {
        "telegram_user_id": str(telegram_user_id),
        "session_type": SESSION_TYPE_EDIT,
        "word_key": word_key,
        "created_at": utc_now_iso(),
        "expires_at": int(time.time()) + EDIT_SESSION_TTL_SECONDS,
        "status": SESSION_STATUS_ACTIVE,
    }
    save_session(session)

    return build_edit_card_prompt(item), build_inline_keyboard(
        (
            ((CANCEL_BUTTON_TEXT, CALLBACK_CANCEL),),
        )
    )


def handle_edit_card_answer(
    session: dict[str, Any],
    user_text: str,
    config: dict[str, str],
) -> tuple[str, dict[str, Any] | None]:
    card = parse_card_input_text(user_text)
    result = update_known_word_from_edit(
        old_word_key=str(session["word_key"]),
        card=card,
        config=config,
    )
    clear_session(str(session["telegram_user_id"]))
    return build_add_result_reply(result)


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

    result = add_card(card, config, destination=DESTINATION_MOCHI, source="api")
    return json_response(200, result.to_dict())


def handle_sync_telegram_commands(event: dict[str, Any]) -> dict[str, Any]:
    config = get_app_config()
    require_secret_header(event, "x-bot-secret", config["APP_SECRET"])
    return json_response(200, sync_telegram_bot_commands(config))


def dispatch_callback(data: str, user_id: str, config: dict[str, str]) -> list[Reply] | None:
    """Returns the messages to send for known callback_data, None for unknown."""
    if data == CALLBACK_INBOX_PROMOTE:
        return build_inbox_promote_replies(config)

    if data.startswith(CALLBACK_TO_MOCHI_PREFIX):
        word_key = build_word_key(deck_id=config["MOCHI_DECK_ID"], normalized_word=data[len(CALLBACK_TO_MOCHI_PREFIX):])
        return [build_add_result_reply(send_known_word_to_mochi(word_key, config))]

    if data.startswith(CALLBACK_TO_INBOX_PREFIX):
        word_key = build_word_key(deck_id=config["MOCHI_DECK_ID"], normalized_word=data[len(CALLBACK_TO_INBOX_PREFIX):])
        return [build_add_result_reply(move_known_word_to_inbox(word_key))]

    if data.startswith(CALLBACK_REGEN_PREFIX):
        result = regenerate_card(data[len(CALLBACK_REGEN_PREFIX):], config)
        return [build_add_result_reply(result)]

    if data.startswith(CALLBACK_EDIT_PREFIX):
        return [start_edit_card_session(data[len(CALLBACK_EDIT_PREFIX):], user_id, config)]

    if data.startswith(CALLBACK_DELETE_CONFIRM_PREFIX):
        return [(delete_known_word(data[len(CALLBACK_DELETE_CONFIRM_PREFIX):], config), None)]

    if data.startswith(CALLBACK_DELETE_PREFIX):
        return [build_delete_confirmation(data[len(CALLBACK_DELETE_PREFIX):], config)]

    if data == CALLBACK_CANCEL:
        clear_session(user_id)
        return [(CANCELLED_TEXT, None)]

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

    try:
        replies = dispatch_callback(data, str(user_id), config)
        if replies is None:
            return
    except UserInputError as error:
        replies = [(str(error), None)]
    except AiGenerationError:
        replies = [(AI_ERROR_TEXT, None)]
    except Exception as error:
        log_event("callback_error", error_type=type(error).__name__)
        replies = [(GENERIC_ERROR_TEXT, None)]

    send_telegram_replies(chat_id=chat_id, replies=replies, config=config)


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

    try:
        replies = process_telegram_text(text, config, telegram_user_id=telegram_user_id)
    except UserInputError as error:
        # Input-format errors quote HELP_TEXT, so attach the help buttons too.
        replies = [(str(error), build_help_menu())]
    except AiGenerationError:
        replies = [(AI_ERROR_TEXT, None)]
    except Exception as error:
        log_event("telegram_processing_error", error_type=type(error).__name__)
        replies = [(GENERIC_ERROR_TEXT, None)]

    send_telegram_replies(chat_id=chat_id, replies=replies, config=config)

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

        if method == "POST" and path == "/sync-telegram-commands":
            return handle_sync_telegram_commands(event)

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
