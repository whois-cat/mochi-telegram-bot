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

from app.keyboards import build_help_menu, build_inline_keyboard, build_practice_task_keyboard
from app.config import (
    CALLBACK_CANCEL,
    CALLBACK_DELETE_CONFIRM_PREFIX,
    CALLBACK_DELETE_PREFIX,
    CALLBACK_EDIT_PREFIX,
    CALLBACK_PRACTICE_HINT,
    CALLBACK_PRACTICE_IDK,
    CALLBACK_REGEN_PREFIX,
    CANCEL_BUTTON_TEXT,
    CANCELLED_TEXT,
    CLOZE_BLANK,
    CONFIRM_DELETE_BUTTON_TEXT,
    DELETE_BUTTON_TEXT,
    EDIT_BUTTON_TEXT,
    EDIT_CARD_PROMPT,
    GEMINI_TIMEOUT_MS,
    GENERIC_ERROR_TEXT,
    HELP_TEXT,
    LEGACY_CARD_SRS_ATTRIBUTES,
    MOCHI_API_URL,
    MOCHI_CARD_TEMPLATE,
    MOCHI_TIMEOUT_SECONDS,
    NOT_ENOUGH_WORDS_TEXT,
    PRACTICE_CORRECT_INTERVAL_STEPS,
    PRACTICE_MAX_PRIORITY_SCORE,
    PRACTICE_BLOCK_QUESTION_COUNT,
    PRACTICE_MODE_TODAY,
    PRACTICE_QUESTION_COUNT,
    PRACTICE_SCAN_LIMIT,
    PRACTICE_SESSION_TTL_SECONDS,
    PRACTICE_SESSION_TYPE_ACTIVE,
    PRACTICE_SESSION_TYPE_EDIT,
    REGENERATE_BUTTON_TEXT,
    RESULT_CORRECT,
    RESULT_HINT_REQUESTED,
    RESULT_IDK,
    RESULT_MINOR_ISSUE,
    RESULT_SKIPPED,
    RESULT_WRONG,
    SESSION_STATUS_ACTIVE,
    STATS_SCAN_LIMIT,
    STATUS_CREATED,
    STATUS_FAILED,
    STATUS_RESERVED,
    TASK_TYPE_FILL_BLANK,
    TASK_TYPE_OWN_SENTENCE,
    TASK_TYPE_TRANSLATE_RU_EN,
    TELEGRAM_BOT_COMMANDS,
    TODAY_PRACTICE_INTRO,
    TELEGRAM_API_BASE,
    TELEGRAM_CALLBACK_DATA_MAX_BYTES,
    TELEGRAM_TIMEOUT_SECONDS,
    USAGE_ALIASES,
    WORD_NOT_FOUND_TEXT,
    get_app_config,
    get_known_words_table_name,
    get_practice_sessions_table_name,
    load_prompt,
)
from app.services.evaluator import (
    evaluate_short_answer_locally,
    format_own_sentence_feedback,
    precheck_own_sentence,
    precheck_translation,
)
from app.services.hint_service import build_hint, is_hint_text, is_idk_text
from app.services.normalization import contains_target_phrase, get_normalized_variants, normalize_text
from app.services.task_generator import build_task_prompt
from app.storage.sessions_repo import (
    COMPACT_STORAGE_VERSION,
    advance_session,
    get_task as get_compact_task,
    get_tasks as get_compact_tasks,
    mark_session_status,
    save_practice_session,
    save_task_result,
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
    cloze_sentence: str | None = None


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
    mode: str  # "manual" | "ai" | "help" | "practice_today" | "stats" | "cancel"
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


def optional_string_field(data: dict[str, Any], field_name: str) -> str | None:
    value = data.get(field_name)

    if value is None:
        return None

    if not isinstance(value, str):
        raise HttpError(400, f"Field '{field_name}' must be a string")

    value = value.strip()
    return value or None


# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------


def normalize_usage_tag(usage: str) -> str:
    normalized = " ".join(usage.strip().lower().split())
    return USAGE_ALIASES.get(normalized, normalized.replace(" ", "-"))


def normalize_word_for_lookup(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    return " ".join(normalized.split())


def sentence_has_exact_word(word: str, sentence: str) -> bool:
    if not word or not sentence:
        return False

    pattern = rf"(?<!\w){re.escape(word)}(?!\w)"
    return re.search(pattern, sentence, flags=re.IGNORECASE) is not None


def infer_cloze_sentence(card: CardInput) -> str | None:
    if card.cloze_sentence and sentence_has_exact_word(card.word, card.cloze_sentence):
        return card.cloze_sentence

    if sentence_has_exact_word(card.word, card.example):
        return card.example

    return None


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


def reserve_known_word(
    *,
    word: str,
    translation: str,
    example: str,
    cloze_sentence: str | None,
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
        "practice_attempt_count": 0,
        "correct_count": 0,
        "wrong_count": 0,
        "minor_issue_count": 0,
        "practice_score": 0,
        "practice_interval_days": 0,
        "next_practice_at": now,
    }
    if cloze_sentence:
        item["cloze_sentence"] = cloze_sentence

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
) -> bool:
    """False when the card no longer exists in Mochi (deleted there manually)."""
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

    cloze_sentence = data.get("cloze_sentence")
    if not isinstance(cloze_sentence, str) or not cloze_sentence.strip():
        log_event("gemini_missing_field", field="cloze_sentence")
        raise AiGenerationError("Gemini response is missing 'cloze_sentence'")
    fields["cloze_sentence"] = cloze_sentence.strip()

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
        "target_word_sentence": str(data.get("target_word_sentence") or "").strip(),
        "natural_alternative": str(data.get("natural_alternative") or "").strip(),
    }


def generate_cloze_tasks_with_gemini(
    items: list[dict[str, Any]],
    support_items: list[dict[str, Any]],
    config: dict[str, str],
) -> dict[str, dict[str, Any]]:
    """Generate fresh cloze sentences keyed by word_key."""
    if genai is None:
        log_event("gemini_sdk_missing")
        raise AiGenerationError("google-genai SDK is not installed")

    target_payload = [
        {
            "id": str(item["word_key"]),
            "word": str(item.get("word") or ""),
            "translation": str(item.get("translation") or ""),
            "usage": str(item.get("usage_tag") or ""),
        }
        for item in items
    ]
    target_words = {str(item.get("word") or "").casefold() for item in items}
    word_bank_payload = [
        {
            "word": str(item.get("word") or ""),
            "translation": str(item.get("translation") or ""),
        }
        for item in support_items
        if str(item.get("word") or "").casefold() not in target_words
    ]

    client = genai.Client(
        api_key=config["GEMINI_API_KEY"],
        http_options=genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )
    prompt = (
        load_prompt("gemini_cloze_tasks")
        .replace("{target_items_json}", json.dumps(target_payload, ensure_ascii=False))
        .replace("{word_bank_json}", json.dumps(word_bank_payload, ensure_ascii=False))
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

    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        log_event("gemini_invalid_shape")
        raise AiGenerationError("Gemini returned an invalid cloze task list")

    generated: dict[str, dict[str, Any]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue

        task_id = str(task.get("id") or "")
        sentence = str(task.get("sentence") or "").strip()
        support_words = task.get("support_words") or []
        if not isinstance(support_words, list):
            support_words = []

        if task_id and sentence:
            generated[task_id] = {
                "sentence": sentence,
                "support_words": [
                    str(word).strip() for word in support_words if str(word).strip()
                ],
            }

    return generated


def generate_translation_tasks_with_gemini(
    items: list[dict[str, Any]],
    config: dict[str, str],
) -> dict[str, dict[str, str]]:
    """Generate sentence translation tasks keyed by word_key."""
    if genai is None:
        log_event("gemini_sdk_missing")
        raise AiGenerationError("google-genai SDK is not installed")

    payload = [
        {
            "id": str(item["word_key"]),
            "word": str(item.get("word") or ""),
            "translation": str(item.get("translation") or ""),
            "example": str(item.get("example") or ""),
        }
        for item in items
    ]

    client = genai.Client(
        api_key=config["GEMINI_API_KEY"],
        http_options=genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )
    prompt = load_prompt("gemini_translation_tasks").replace(
        "{items_json}",
        json.dumps(payload, ensure_ascii=False),
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

    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, list):
        log_event("gemini_invalid_shape")
        raise AiGenerationError("Gemini returned an invalid task list")

    generated: dict[str, dict[str, str]] = {}
    for task in tasks:
        if not isinstance(task, dict):
            continue

        task_id = str(task.get("id") or "")
        russian_sentence = str(task.get("russian_sentence") or "").strip()
        expected_english = str(task.get("expected_english") or "").strip()
        accepted_answers = task.get("accepted_answers") or []
        if not isinstance(accepted_answers, list):
            accepted_answers = []

        if task_id and russian_sentence and expected_english:
            generated[task_id] = {
                "russian_sentence": russian_sentence,
                "expected_english": expected_english,
                "accepted_answers": [
                    str(answer).strip() for answer in accepted_answers if str(answer).strip()
                ],
            }

    return generated


def evaluate_translation_with_gemini(
    task: dict[str, Any],
    user_answer: str,
    config: dict[str, str],
) -> dict[str, str]:
    """Evaluate a sentence translation by meaning."""
    if genai is None:
        log_event("gemini_sdk_missing")
        raise AiGenerationError("google-genai SDK is not installed")

    client = genai.Client(
        api_key=config["GEMINI_API_KEY"],
        http_options=genai_types.HttpOptions(timeout=GEMINI_TIMEOUT_MS),
    )
    prompt = (
        load_prompt("gemini_translation_eval")
        .replace("{russian_sentence}", str(task.get("russian_sentence") or ""))
        .replace("{expected_english}", str(task.get("expected_answer") or ""))
        .replace("{word}", str(task.get("word") or ""))
        .replace("{answer}", user_answer)
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

    if not isinstance(data, dict) or data.get("result") not in ("correct", "minor_issue", "wrong"):
        log_event("gemini_invalid_shape")
        raise AiGenerationError("Gemini returned an invalid translation evaluation")

    return {
        "result": str(data["result"]),
        "feedback": str(data.get("feedback") or "").strip(),
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
    cloze_sentence = infer_cloze_sentence(card)

    reservation = reserve_known_word(
        word=card.word,
        translation=card.translation,
        example=card.example,
        cloze_sentence=cloze_sentence,
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
    cloze_sentence = infer_cloze_sentence(card)

    # DynamoDB keeps Telegram's local index; Mochi remains the card system.
    # If the linked Mochi card is gone, create a new one and relink it.
    mochi_card_id = str(item.get("mochi_card_id") or "")
    if not mochi_card_id or not update_mochi_card(mochi_card_id, card, usage_tag, config):
        mochi_card_id = str(create_mochi_card(card, usage_tag, config).get("id") or "")

    update_expression = (
        "SET #translation = :translation, example = :example, "
        "usage_tag = :usage_tag, mochi_card_id = :mochi_card_id, "
        "#status = :status, updated_at = :updated_at"
    )
    expression_values = {
        ":translation": card.translation,
        ":example": card.example,
        ":usage_tag": usage_tag,
        ":mochi_card_id": mochi_card_id,
        ":status": STATUS_CREATED,
        ":updated_at": utc_now_iso(),
    }
    if cloze_sentence:
        update_expression += ", cloze_sentence = :cloze_sentence"
        expression_values[":cloze_sentence"] = cloze_sentence

    update_expression += remove_expression_for(LEGACY_CARD_SRS_ATTRIBUTES)

    table.update_item(
        Key={"word_key": word_key},
        # "translation" and "status" are DynamoDB reserved keywords.
        UpdateExpression=update_expression,
        ExpressionAttributeNames={"#translation": "translation", "#status": "status"},
        ExpressionAttributeValues=expression_values,
    )

    log_event("card_regenerated", has_mochi_card=bool(mochi_card_id))

    return AddCardResult(
        created=True,
        word=word,
        mochi_card_id=mochi_card_id or None,
        translation=card.translation,
        example=card.example,
    )


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
        "status": STATUS_CREATED,
        "updated_at": now,
    }
    item.setdefault("created_at", now)

    cloze_sentence = infer_cloze_sentence(card)
    if cloze_sentence:
        item["cloze_sentence"] = cloze_sentence
    else:
        item.pop("cloze_sentence", None)

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

    usage_tag = normalize_usage_tag(card.usage)
    new_word_key = word_key_for(card.word, config)

    if new_word_key != old_word_key:
        collision = table.get_item(Key={"word_key": new_word_key}, ConsistentRead=True).get("Item")
        if collision:
            raise UserInputError("A card with this word already exists.")

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

        if "cloze_sentence" in item:
            update_expression += ", cloze_sentence = :cloze_sentence"
            expression_values[":cloze_sentence"] = item["cloze_sentence"]
            remove_attrs = LEGACY_CARD_SRS_ATTRIBUTES
        else:
            remove_attrs = (  # also remove an invalid cloze sentence when the edit cannot support it
                "cloze_sentence",
                *LEGACY_CARD_SRS_ATTRIBUTES,
            )

        update_expression += remove_expression_for(remove_attrs)

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

    return AddCardResult(
        created=True,
        word=card.word,
        mochi_card_id=mochi_card_id or None,
        translation=card.translation,
        example=card.example,
    )


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


def build_card_actions_keyboard(word: str) -> dict[str, Any] | None:
    regen = callback_data_for(CALLBACK_REGEN_PREFIX, word)
    edit = callback_data_for(CALLBACK_EDIT_PREFIX, word)
    delete = callback_data_for(CALLBACK_DELETE_PREFIX, word)

    if not regen or not edit or not delete:
        return None

    return {
        "inline_keyboard": [
            [
                {"text": REGENERATE_BUTTON_TEXT, "callback_data": regen},
                {"text": EDIT_BUTTON_TEXT, "callback_data": edit},
                {"text": DELETE_BUTTON_TEXT, "callback_data": delete},
            ]
        ]
    }


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
    return f"Delete <b>{html.escape(word)}</b>? It will also be removed from Mochi.", keyboard


def parse_telegram_command(text: str) -> TelegramCommand:
    """
    Supported:
      /add word | translation | usage | example  -> mode="manual"
      /ai word_or_phrase                         -> mode="ai"
      /delete word                               -> mode="delete"
      /help, /start                              -> mode="help"
      /today                                     -> mode="practice_today"
      /stats                                     -> mode="stats"
      /cancel                                    -> mode="cancel"
    Anything else raises UserInputError with the format help.
    """
    text = text.strip()
    command, _, rest = text.partition(" ")
    command = command.split("@", 1)[0].lower()  # supports /add@YourBotName
    rest = rest.strip()

    if command in ("/help", "/start"):
        return TelegramCommand(mode="help")

    if command == "/cancel":
        return TelegramCommand(mode="cancel")

    if command == "/today":
        return TelegramCommand(mode="practice_today")

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

    return "\n".join(lines), build_card_actions_keyboard(result.word)


def process_telegram_text(
    text: str,
    config: dict[str, str],
    *,
    telegram_user_id: str,
) -> tuple[str, dict[str, Any] | None]:
    """Returns (reply_text, reply_markup or None)."""
    stripped = text.strip()

    session = get_active_session(telegram_user_id)
    if session and stripped.lower() == "/cancel":
        clear_session(telegram_user_id)
        return CANCELLED_TEXT, None

    # Plain text while a session is active is either a practice answer or edit payload.
    if session and not stripped.startswith("/"):
        if session.get("session_type") == PRACTICE_SESSION_TYPE_EDIT:
            return handle_edit_card_answer(session, stripped, config)
        return handle_practice_answer(session, stripped, config)

    command = parse_telegram_command(stripped)
    log_event("telegram_command", mode=command.mode)

    if command.mode == "cancel":
        clear_session(telegram_user_id)
        return CANCELLED_TEXT, None

    if command.mode == "help":
        return HELP_TEXT, build_help_menu()

    if command.mode == "practice_today":
        return start_today_practice_session(telegram_user_id, config)

    if command.mode == "stats":
        return build_stats_reply(), None

    if command.mode == "delete":
        return build_delete_confirmation(normalize_word_for_lookup(command.query), config)

    if command.mode == "manual":
        return build_add_result_reply(add_card(command.card, config, source="telegram"))

    return build_add_result_reply(add_ai_card(command.query, config))


# ---------------------------------------------------------------------------
# Active practice: word selection
# ---------------------------------------------------------------------------


def scan_known_word_items(limit: int = PRACTICE_SCAN_LIMIT) -> list[dict[str, Any]]:
    """Scan is fine here: small personal table (see project constraints)."""
    table = get_known_words_table()
    items: list[dict[str, Any]] = []
    last_evaluated_key = None

    while True:
        scan_kwargs: dict[str, Any] = {}

        if limit:
            remaining = limit - len(items)
            if remaining <= 0:
                log_event("practice_scan_capped", limit=limit)
                break
            scan_kwargs["Limit"] = remaining

        if last_evaluated_key:
            scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items") or [])
        last_evaluated_key = response.get("LastEvaluatedKey")

        if not last_evaluated_key:
            break

    return items


def get_known_words_for_practice(limit: int = PRACTICE_SCAN_LIMIT) -> list[dict[str, Any]]:
    return [item for item in scan_known_word_items(limit=limit) if item.get("status") == STATUS_CREATED]


def build_cloze_sentence(
    word: str,
    example: str,
    cloze_sentence: str | None = None,
) -> str | None:
    """Blank the target word out of its cloze sentence or example."""
    if not word:
        return None

    pattern = rf"(?<!\w){re.escape(word)}(?!\w)"

    for sentence in (cloze_sentence, example):
        if not sentence:
            continue

        blanked, count = re.subn(pattern, CLOZE_BLANK, sentence, flags=re.IGNORECASE)
        if count:
            return blanked

    return None


def word_fits_active_practice(item: dict[str, Any]) -> bool:
    word = str(item.get("word") or "")
    return bool(word and item.get("translation"))


def word_fits_blank_practice(item: dict[str, Any]) -> bool:
    return word_fits_active_practice(item)


def parse_iso_datetime(value: Any) -> datetime | None:
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)

    return parsed


def practice_candidate_score(item: dict[str, Any], now: datetime) -> float:
    correct_count = int(item.get("correct_count") or 0)
    wrong_count = int(item.get("wrong_count") or 0)
    minor_issue_count = int(item.get("minor_issue_count") or 0)
    idk_count = int(item.get("idk_count") or 0)
    practice_score = int(item.get("practice_score") or 0)
    last_practiced_at = parse_iso_datetime(item.get("last_practiced_at"))
    next_practice_at = parse_iso_datetime(item.get("next_practice_at"))

    score = random.random()
    score += wrong_count * 3
    score += idk_count * 3
    score += minor_issue_count * 2
    score += practice_score * 2
    score -= correct_count * 0.4

    if not last_practiced_at:
        score += 5
    else:
        hours_since_practice = max((now - last_practiced_at).total_seconds() / 3600, 0)
        if hours_since_practice < 24:
            score -= 5
        else:
            score += min(hours_since_practice / 24, 7) * 0.5

    if not next_practice_at:
        score += 4
    elif next_practice_at <= now:
        days_ready = max((now - next_practice_at).total_seconds() / 86400, 0)
        score += 4 + min(days_ready, 7)
    else:
        hours_until_ready = max((next_practice_at - now).total_seconds() / 3600, 0)
        score -= min(hours_until_ready / 24, 5)

    return score


def choose_practice_block(
    candidates: list[dict[str, Any]],
    limit: int,
    *,
    excluded_word_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    excluded_word_keys = excluded_word_keys or set()
    now = datetime.now(timezone.utc)
    available = [
        item for item in candidates if str(item.get("word_key") or "") not in excluded_word_keys
    ]
    ranked = sorted(
        available,
        key=lambda item: practice_candidate_score(item, now),
        reverse=True,
    )
    return ranked[:limit]


def build_today_practice_questions(
    words: list[dict[str, Any]],
    support_words: list[dict[str, Any]],
    config: dict[str, str],
) -> list[dict[str, Any]]:
    generated_tasks = generate_cloze_tasks_with_gemini(words, support_words, config)
    questions = []

    for item in words:
        word_key = str(item["word_key"])
        word = str(item.get("word") or "")
        translation = str(item.get("translation") or "")
        generated = generated_tasks.get(word_key)
        if not generated:
            raise AiGenerationError("Gemini did not generate all cloze tasks")

        generated_sentence = str(generated.get("sentence") or "")
        blanked = build_cloze_sentence(word, generated_sentence)
        if not blanked:
            raise AiGenerationError("Gemini generated a cloze sentence without the target word")

        questions.append(
            {
                "task_type": TASK_TYPE_FILL_BLANK,
                "linked_word_keys": [word_key],
                "target_word_keys": [word_key],
                "word": word,
                "translation": translation,
                "prompt": f"{blanked} ({translation})",
                "source_sentence": generated_sentence,
                "support_words": generated.get("support_words") or [],
                "expected_answer": word,
                "accepted_answers": [word],
                "acceptable_answers": [word],
            }
        )

    return questions


def build_translation_practice_questions(
    words: list[dict[str, Any]],
    config: dict[str, str],
) -> list[dict[str, Any]]:
    generated_tasks = generate_translation_tasks_with_gemini(words, config)
    questions = []
    for item in words:
        word_key = str(item["word_key"])
        word = str(item.get("word") or "")
        generated = generated_tasks.get(word_key)
        if not generated:
            raise AiGenerationError("Gemini did not generate all translation tasks")

        russian_sentence = generated["russian_sentence"]
        expected_english = generated["expected_english"]
        accepted_answers = [
            expected_english,
            *generated.get("accepted_answers", []),
            *sorted(get_normalized_variants(expected_english)),
        ]

        questions.append(
            {
                "task_type": TASK_TYPE_TRANSLATE_RU_EN,
                "linked_word_keys": [word_key],
                "target_word_keys": [word_key],
                "word": word,
                "translation": str(item.get("translation") or ""),
                "russian_sentence": russian_sentence,
                "prompt": f"Переведи на английский:\n{russian_sentence}",
                "expected_answer": expected_english,
                "accepted_answers": list(dict.fromkeys(accepted_answers)),
                "acceptable_answers": [expected_english],
            }
        )

    return questions


def build_own_sentence_practice_questions(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "task_type": TASK_TYPE_OWN_SENTENCE,
            "linked_word_keys": [item["word_key"]],
            "target_word_keys": [item["word_key"]],
            "word": str(item.get("word") or ""),
            "translation": str(item.get("translation") or ""),
            "example": str(item.get("example") or ""),
            "prompt": f"Напиши свое предложение со словом/фразой: {item.get('word')}",
            "expected_answer": str(item.get("word") or ""),
            "accepted_answers": [str(item.get("word") or "")],
            "acceptable_answers": [str(item.get("word") or "")],
        }
        for item in words
    ]


def build_today_practice_tasks(config: dict[str, str]) -> list[dict[str, Any]]:
    words = [item for item in get_known_words_for_practice() if word_fits_active_practice(item)]
    blank_candidates = [item for item in words if word_fits_blank_practice(item)]
    used_word_keys: set[str] = set()

    blank_words = choose_practice_block(
        blank_candidates,
        PRACTICE_BLOCK_QUESTION_COUNT,
        excluded_word_keys=used_word_keys,
    )
    used_word_keys.update(str(item["word_key"]) for item in blank_words)

    translation_words = choose_practice_block(
        words,
        PRACTICE_BLOCK_QUESTION_COUNT,
        excluded_word_keys=used_word_keys,
    )
    used_word_keys.update(str(item["word_key"]) for item in translation_words)

    own_sentence_words = choose_practice_block(
        words,
        PRACTICE_BLOCK_QUESTION_COUNT,
        excluded_word_keys=used_word_keys,
    )

    if (
        len(blank_words) < PRACTICE_BLOCK_QUESTION_COUNT
        or len(translation_words) < PRACTICE_BLOCK_QUESTION_COUNT
        or len(own_sentence_words) < PRACTICE_BLOCK_QUESTION_COUNT
    ):
        return []

    tasks = []
    support_words = [*translation_words, *own_sentence_words, *blank_words]
    tasks.extend(build_today_practice_questions(blank_words, support_words, config))
    tasks.extend(build_translation_practice_questions(translation_words, config))
    tasks.extend(build_own_sentence_practice_questions(own_sentence_words))
    return tasks


def start_today_practice_session(
    telegram_user_id: str,
    config: dict[str, str],
) -> tuple[str, dict[str, Any] | None]:
    tasks = build_today_practice_tasks(config)

    if len(tasks) < PRACTICE_QUESTION_COUNT:
        return NOT_ENOUGH_WORDS_TEXT, None

    session = {
        "telegram_user_id": str(telegram_user_id),
        "user_id": str(telegram_user_id),
        "training_id": f"{telegram_user_id}:{int(time.time())}",
        "session_type": PRACTICE_SESSION_TYPE_ACTIVE,
        "storage_version": COMPACT_STORAGE_VERSION,
        "mode": PRACTICE_MODE_TODAY,
        "tasks": tasks,
        "current_task_index": 0,
        "user_answers": [],
        "evaluation_results": [],
        "created_at": utc_now_iso(),
        "expires_at": int(time.time()) + PRACTICE_SESSION_TTL_SECONDS,
        "status": SESSION_STATUS_ACTIVE,
    }
    save_session(str(telegram_user_id), session)

    log_event("active_practice_started", mode=PRACTICE_MODE_TODAY, tasks=len(tasks))
    return (
        "\n\n".join(
            [
                TODAY_PRACTICE_INTRO,
                build_task_prompt(tasks[0], 1, len(tasks)),
            ]
        ),
        build_practice_task_keyboard(),
    )


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


def get_practice_sessions_table():
    return boto3.resource("dynamodb").Table(get_practice_sessions_table_name())


def save_session(telegram_user_id: str, session: dict[str, Any]) -> None:
    if (
        session.get("session_type") == PRACTICE_SESSION_TYPE_ACTIVE
        and session.get("storage_version") == COMPACT_STORAGE_VERSION
        and session.get("tasks")
    ):
        save_practice_session(
            get_practice_sessions_table(),
            user_id=str(telegram_user_id),
            session=session,
            tasks=list(session.get("tasks") or []),
        )
        return

    get_practice_sessions_table().put_item(Item=session)


def get_active_session(telegram_user_id: str) -> dict[str, Any] | None:
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


def clear_session(telegram_user_id: str) -> None:
    table = get_practice_sessions_table()
    item = table.get_item(Key={"telegram_user_id": str(telegram_user_id)}, ConsistentRead=True).get("Item")

    if item and item.get("storage_version") == COMPACT_STORAGE_VERSION:
        mark_session_status(table, user_id=str(telegram_user_id), status="cancelled")
        return

    table.delete_item(Key={"telegram_user_id": str(telegram_user_id)})


# ---------------------------------------------------------------------------
# Active practice: scoring
# ---------------------------------------------------------------------------


def next_correct_interval(current_interval_days: int) -> int:
    for interval_days in PRACTICE_CORRECT_INTERVAL_STEPS:
        if current_interval_days < interval_days:
            return interval_days

    return PRACTICE_CORRECT_INTERVAL_STEPS[-1]


def calculate_next_active_practice(
    result: str,
    current_score: int,
    current_interval_days: int,
) -> tuple[int, int]:
    if result == RESULT_CORRECT:
        practice_score = max(current_score - 1, 0)
        interval_days = next_correct_interval(current_interval_days)
    elif result == RESULT_MINOR_ISSUE:
        practice_score = min(current_score + 1, PRACTICE_MAX_PRIORITY_SCORE)
        interval_days = min(max(current_interval_days, 1), 3)
    else:
        practice_score = min(current_score + 2, PRACTICE_MAX_PRIORITY_SCORE)
        interval_days = 0

    return practice_score, interval_days


def update_word_practice_stats(word_key: str, result: str) -> None:
    table = get_known_words_table()
    item = table.get_item(Key={"word_key": word_key}).get("Item") or {}
    practice_score, interval_days = calculate_next_active_practice(
        result,
        int(item.get("practice_score") or 0),
        int(item.get("practice_interval_days") or 0),
    )
    now = datetime.now(timezone.utc)
    set_parts = [
        "practice_attempt_count = if_not_exists(practice_attempt_count, :zero) + :one",
        "last_practiced_at = :now",
        "last_practice_result = :result",
        "practice_score = :practice_score",
        "practice_interval_days = :interval",
        "next_practice_at = :next_practice_at",
        "updated_at = :now",
    ]
    expression_values = {
        ":zero": 0,
        ":one": 1,
        ":now": now.isoformat(),
        ":result": result,
        ":practice_score": practice_score,
        ":interval": interval_days,
        ":next_practice_at": (now + timedelta(days=interval_days)).isoformat(),
    }

    if result == RESULT_HINT_REQUESTED:
        return

    if result == RESULT_CORRECT:
        set_parts.append("correct_count = if_not_exists(correct_count, :zero) + :one")
        set_parts.append("last_correct_at = :now")
    elif result == RESULT_MINOR_ISSUE:
        set_parts.append("minor_issue_count = if_not_exists(minor_issue_count, :zero) + :one")
        set_parts.append("last_minor_issue_at = :now")
    elif result == RESULT_IDK:
        set_parts.append("idk_count = if_not_exists(idk_count, :zero) + :one")
        set_parts.append("last_idk_at = :now")
    else:
        set_parts.append("wrong_count = if_not_exists(wrong_count, :zero) + :one")
        set_parts.append("last_wrong_at = :now")

    table.update_item(
        Key={"word_key": word_key},
        UpdateExpression="SET " + ", ".join(set_parts) + remove_expression_for(LEGACY_CARD_SRS_ATTRIBUTES),
        ExpressionAttributeValues=expression_values,
    )


# ---------------------------------------------------------------------------
# Active practice: answer evaluation
# ---------------------------------------------------------------------------


def normalize_answer(value: str) -> str:
    return normalize_text(value)


def answer_matches(expected: str, answer: str) -> bool:
    return answer_match_result(expected, answer) in (RESULT_CORRECT, RESULT_MINOR_ISSUE)


def answer_match_result(
    expected: str,
    answer: str,
    accepted_answers: list[str] | None = None,
) -> str:
    task = {
        "expected_answer": expected,
        "accepted_answers": accepted_answers or [
            option.strip() for option in expected.split(",") if option.strip()
        ],
    }
    return evaluate_short_answer_locally(task, answer)["result"]


def evaluate_fill_blank_answer(task: dict[str, Any], user_answer: str) -> dict[str, str]:
    return evaluate_short_answer_locally(task, user_answer)


def evaluate_own_sentence_answer(
    task: dict[str, Any],
    user_answer: str,
    config: dict[str, str],
) -> dict[str, str]:
    precheck = precheck_own_sentence(task, user_answer)
    if precheck:
        return precheck

    evaluation = evaluate_sentence_with_gemini(str(task.get("word") or ""), user_answer, config)
    result_map = {
        "good": RESULT_CORRECT,
        "minor_issue": RESULT_MINOR_ISSUE,
        "wrong_usage": RESULT_WRONG,
    }
    result = result_map[evaluation["result"]]
    target_word_sentence = (
        evaluation.get("target_word_sentence")
        or evaluation.get("better_sentence")
        or ""
    )
    if target_word_sentence and not contains_target_phrase(target_word_sentence, str(task.get("word") or "")):
        target_word_sentence = ""
    feedback = format_own_sentence_feedback(
        word=str(task.get("word") or ""),
        result=result,
        feedback=evaluation.get("feedback") or "",
        target_word_sentence=target_word_sentence,
        natural_alternative=evaluation.get("natural_alternative") or "",
    )

    return {
        "result": result,
        "feedback": feedback or evaluation["result"],
    }


def evaluate_training_task(
    task: dict[str, Any],
    user_answer: str,
    config: dict[str, str],
) -> dict[str, str]:
    task_type = task.get("task_type")

    if task_type == TASK_TYPE_FILL_BLANK:
        return evaluate_fill_blank_answer(task, user_answer)

    if task_type == TASK_TYPE_TRANSLATE_RU_EN:
        precheck = precheck_translation(task, user_answer)
        if precheck:
            return precheck
        return evaluate_translation_with_gemini(task, user_answer, config)

    if task_type == TASK_TYPE_OWN_SENTENCE:
        return evaluate_own_sentence_answer(task, user_answer, config)

    raise UserInputError("Unknown practice task.")


def result_label(result: str) -> str:
    if result == RESULT_CORRECT:
        return "Correct"
    if result == RESULT_MINOR_ISSUE:
        return "Minor issue"
    if result == RESULT_IDK:
        return "IDK"
    if result == RESULT_SKIPPED:
        return "Skipped"
    if result == RESULT_HINT_REQUESTED:
        return "Hint"
    return "Wrong"


def update_practice_stats_for_task(task: dict[str, Any], result: str) -> None:
    if result == RESULT_HINT_REQUESTED:
        return

    for word_key in task.get("target_word_keys") or task.get("linked_word_keys") or []:
        update_word_practice_stats(str(word_key), result)


def is_compact_practice_session(session: dict[str, Any]) -> bool:
    return session.get("storage_version") == COMPACT_STORAGE_VERSION


def load_session_task(session: dict[str, Any], task_index: int) -> dict[str, Any] | None:
    if is_compact_practice_session(session):
        return get_compact_task(
            get_practice_sessions_table(),
            user_id=str(session["telegram_user_id"]),
            session_id=str(session["session_id"]),
            task_index=task_index,
        )

    tasks = session.get("tasks") or []
    if 0 <= task_index < len(tasks):
        return tasks[task_index]

    return None


def load_session_tasks(session: dict[str, Any]) -> list[dict[str, Any]]:
    if is_compact_practice_session(session):
        return get_compact_tasks(
            get_practice_sessions_table(),
            user_id=str(session["telegram_user_id"]),
            session_id=str(session["session_id"]),
            total_tasks=int(session.get("total_tasks") or 0),
        )

    return list(session.get("tasks") or [])


def task_result_for_summary(task: dict[str, Any]) -> str:
    return str(task.get("evaluation_result") or task.get("result") or "")


def build_training_summary(session: dict[str, Any], tasks: list[dict[str, Any]] | None = None) -> str:
    tasks = tasks if tasks is not None else load_session_tasks(session)
    legacy_results = {
        int(item.get("task_index") or 0): str(item.get("result") or "")
        for item in session.get("evaluation_results") or []
    }
    results = [
        task_result_for_summary(task) or legacy_results.get(index, "")
        for index, task in enumerate(tasks)
    ]
    correct = sum(1 for result in results if result == RESULT_CORRECT)
    minor = sum(1 for result in results if result == RESULT_MINOR_ISSUE)
    wrong = sum(1 for result in results if result == RESULT_WRONG)
    idk = sum(1 for result in results if result == RESULT_IDK)
    skipped = sum(1 for result in results if result == RESULT_SKIPPED)

    practice_again = []
    for task, result in zip(tasks, results):
        if result in (RESULT_CORRECT, RESULT_HINT_REQUESTED, ""):
            continue

        word = str(task.get("word") or "")
        if word and word not in practice_again:
            practice_again.append(word)

    lines = [
        "Готово.",
        "",
        f"Correct: {correct}",
        f"Minor issues: {minor}",
        f"Wrong: {wrong}",
    ]
    if idk:
        lines.append(f"IDK: {idk}")
    if skipped:
        lines.append(f"Skipped: {skipped}")

    if practice_again:
        lines += ["", "Слова, которые стоит потренировать еще:"]
        lines += [f"- {html.escape(word)}" for word in practice_again]

    return "\n".join(lines)


def handle_practice_answer(
    session: dict[str, Any],
    user_text: str,
    config: dict[str, str],
) -> tuple[str, dict[str, Any] | None]:
    total_tasks = int(session.get("total_tasks") or len(session.get("tasks") or []))
    current_task_index = int(session.get("current_task_index") or 0)

    if current_task_index >= total_tasks:
        tasks = load_session_tasks(session)
        if is_compact_practice_session(session):
            mark_session_status(
                get_practice_sessions_table(),
                user_id=str(session["telegram_user_id"]),
                status="completed",
            )
        else:
            clear_session(str(session["telegram_user_id"]))
        return build_training_summary(session, tasks), None

    task = load_session_task(session, current_task_index)
    if not task:
        clear_session(str(session["telegram_user_id"]))
        return GENERIC_ERROR_TEXT, None

    if is_hint_text(user_text):
        hint = build_hint(task)
        return (
            "\n\n".join(
                [
                    html.escape(hint),
                    build_task_prompt(task, current_task_index + 1, total_tasks),
                ]
            ),
            build_practice_task_keyboard(),
        )

    if is_idk_text(user_text):
        evaluation = {
            "result": RESULT_IDK,
            "feedback": f"Correct answer: {task.get('expected_answer')}",
        }
    else:
        evaluation = evaluate_training_task(task, user_text, config)

    result = evaluation["result"]
    update_practice_stats_for_task(task, result)

    next_task_index = current_task_index + 1
    completed = next_task_index >= total_tasks

    if is_compact_practice_session(session):
        table = get_practice_sessions_table()
        save_task_result(
            table,
            user_id=str(session["telegram_user_id"]),
            session_id=str(session["session_id"]),
            task_index=current_task_index,
            user_answer=user_text,
            evaluation=evaluation,
        )
        advance_session(
            table,
            user_id=str(session["telegram_user_id"]),
            next_task_index=next_task_index,
            result=result,
            completed=completed,
        )
        updated_session = {
            **session,
            "current_task_index": next_task_index,
        }
    else:
        user_answers = list(session.get("user_answers") or [])
        evaluation_results = list(session.get("evaluation_results") or [])
        user_answers.append(
            {
                "task_index": current_task_index,
                "answer": user_text,
                "answered_at": utc_now_iso(),
            }
        )
        evaluation_results.append(
            {
                "task_index": current_task_index,
                "task_type": task.get("task_type"),
                "result": result,
                "feedback": evaluation.get("feedback") or "",
                "evaluated_at": utc_now_iso(),
            }
        )
        updated_session = {
            **session,
            "current_task_index": next_task_index,
            "user_answers": user_answers,
            "evaluation_results": evaluation_results,
            "updated_at": utc_now_iso(),
        }

    feedback_lines = [
        f"{result_label(result)}.",
    ]
    if evaluation.get("feedback"):
        feedback_lines.append(html.escape(evaluation["feedback"]))

    if completed:
        if not is_compact_practice_session(updated_session):
            updated_session["completed_at"] = utc_now_iso()
            clear_session(str(session["telegram_user_id"]))
        tasks = load_session_tasks(updated_session)
        return "\n".join(feedback_lines + ["", build_training_summary(updated_session, tasks)]), None

    next_task = load_session_task(updated_session, next_task_index)
    if not next_task:
        clear_session(str(session["telegram_user_id"]))
        return "\n".join(feedback_lines + ["", GENERIC_ERROR_TEXT]), None

    if not is_compact_practice_session(updated_session):
        save_session(str(session["telegram_user_id"]), updated_session)

    return "\n\n".join(
        [
            "\n".join(feedback_lines),
            build_task_prompt(next_task, next_task_index + 1, total_tasks),
        ]
    ), build_practice_task_keyboard()


# ---------------------------------------------------------------------------
# Edit sessions
# ---------------------------------------------------------------------------


def parse_card_input_text(text: str) -> CardInput:
    parts = [part.strip() for part in text.split("|")]

    if len(parts) not in (4, 5) or any(not part for part in parts[:4]):
        raise UserInputError(EDIT_CARD_PROMPT)

    cloze_sentence = parts[4] if len(parts) == 5 and parts[4] else None
    return CardInput(
        word=parts[0],
        translation=parts[1],
        usage=parts[2],
        example=parts[3],
        cloze_sentence=cloze_sentence,
    )


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
        "session_type": PRACTICE_SESSION_TYPE_EDIT,
        "word_key": word_key,
        "created_at": utc_now_iso(),
        "expires_at": int(time.time()) + PRACTICE_SESSION_TTL_SECONDS,
        "status": SESSION_STATUS_ACTIVE,
    }
    save_session(str(telegram_user_id), session)

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
    return build_add_result_reply(result, title="Updated")


# ---------------------------------------------------------------------------
# Active practice: stats
# ---------------------------------------------------------------------------


def build_stats_reply() -> str:
    words = get_known_words_for_practice(limit=STATS_SCAN_LIMIT)
    practiced = sum(1 for item in words if int(item.get("practice_attempt_count") or 0) > 0)
    attempts = sum(int(item.get("practice_attempt_count") or 0) for item in words)
    correct = sum(int(item.get("correct_count") or 0) for item in words)
    minor = sum(int(item.get("minor_issue_count") or 0) for item in words)
    wrong = sum(int(item.get("wrong_count") or 0) for item in words)
    idk = sum(int(item.get("idk_count") or 0) for item in words)
    accuracy = round(correct / attempts * 100) if attempts else 0

    return (
        f"Total words: {len(words)}\n"
        f"Practiced words: {practiced}\n"
        f"Practice attempts: {attempts}\n"
        f"Correct: {correct}\n"
        f"Minor issues: {minor}\n"
        f"Wrong: {wrong}\n"
        f"IDK: {idk}\n"
        f"Practice accuracy: {accuracy}%"
    )


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def migrate_legacy_practice_item(item: dict[str, Any], now_iso: str) -> dict[str, Any]:
    needs_migration = any(attribute in item for attribute in LEGACY_CARD_SRS_ATTRIBUTES)
    needs_migration = needs_migration or any(
        attribute not in item
        for attribute in (
            "practice_attempt_count",
            "correct_count",
            "wrong_count",
            "minor_issue_count",
            "idk_count",
            "practice_score",
            "practice_interval_days",
            "next_practice_at",
        )
    )

    if not needs_migration:
        return item

    migrated = dict(item)
    correct_count = int(migrated.get("correct_count") or 0)
    wrong_count = int(migrated.get("wrong_count") or 0)
    minor_issue_count = int(migrated.get("minor_issue_count") or 0)
    idk_count = int(migrated.get("idk_count") or 0)

    migrated.setdefault("practice_attempt_count", int(migrated.get("review_count") or 0))
    migrated.setdefault("correct_count", correct_count)
    migrated.setdefault("wrong_count", wrong_count)
    migrated.setdefault("minor_issue_count", minor_issue_count)
    migrated.setdefault("idk_count", idk_count)
    migrated.setdefault(
        "practice_score",
        min(wrong_count * 2 + minor_issue_count, PRACTICE_MAX_PRIORITY_SCORE),
    )
    migrated.setdefault("practice_interval_days", 0)
    migrated.setdefault("next_practice_at", now_iso)
    migrated["updated_at"] = now_iso

    for attribute in LEGACY_CARD_SRS_ATTRIBUTES:
        migrated.pop(attribute, None)

    return migrated


def migrate_legacy_practice_stats() -> dict[str, Any]:
    table = get_known_words_table()
    now_iso = utc_now_iso()
    scanned = 0
    migrated_count = 0

    for item in scan_known_word_items():
        scanned += 1
        migrated = migrate_legacy_practice_item(item, now_iso)

        if migrated == item:
            continue

        table.put_item(Item=migrated)
        migrated_count += 1

    log_event("legacy_practice_stats_migrated", scanned=scanned, migrated=migrated_count)
    return {"ok": True, "scanned": scanned, "migrated": migrated_count}


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
        cloze_sentence=optional_string_field(data, "cloze_sentence"),
    )

    result = add_card(card, config, source="api")
    return json_response(200, result.to_dict())


def handle_migrate_practice_stats(event: dict[str, Any]) -> dict[str, Any]:
    config = get_app_config()
    require_secret_header(event, "x-bot-secret", config["APP_SECRET"])
    return json_response(200, migrate_legacy_practice_stats())


def handle_sync_telegram_commands(event: dict[str, Any]) -> dict[str, Any]:
    config = get_app_config()
    require_secret_header(event, "x-bot-secret", config["APP_SECRET"])
    return json_response(200, sync_telegram_bot_commands(config))


def dispatch_callback(data: str, user_id: str, config: dict[str, str]) -> tuple[str, dict[str, Any] | None] | None:
    """Returns (reply, reply_markup) for known callback_data, None for unknown."""
    if data.startswith(CALLBACK_REGEN_PREFIX):
        result = regenerate_card(data[len(CALLBACK_REGEN_PREFIX):], config)
        return build_add_result_reply(result, title="Regenerated")

    if data.startswith(CALLBACK_EDIT_PREFIX):
        return start_edit_card_session(data[len(CALLBACK_EDIT_PREFIX):], user_id, config)

    if data.startswith(CALLBACK_DELETE_CONFIRM_PREFIX):
        return delete_known_word(data[len(CALLBACK_DELETE_CONFIRM_PREFIX):], config), None

    if data.startswith(CALLBACK_DELETE_PREFIX):
        return build_delete_confirmation(data[len(CALLBACK_DELETE_PREFIX):], config)

    if data == CALLBACK_CANCEL:
        clear_session(user_id)
        return CANCELLED_TEXT, None

    if not data.startswith("practice:"):
        return None

    choice = data.split(":", 1)[1]
    log_event("practice_callback", choice=choice)

    if data == CALLBACK_PRACTICE_HINT:
        session = get_active_session(user_id)
        if not session or session.get("session_type") != PRACTICE_SESSION_TYPE_ACTIVE:
            return "No active practice session.", None
        return handle_practice_answer(session, "hint", config)

    if data == CALLBACK_PRACTICE_IDK:
        session = get_active_session(user_id)
        if not session or session.get("session_type") != PRACTICE_SESSION_TYPE_ACTIVE:
            return "No active practice session.", None
        return handle_practice_answer(session, "idk", config)

    if choice == "stats":
        return build_stats_reply(), None

    if choice == "today":
        return start_today_practice_session(user_id, config)

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
        reply = "AI could not complete this request. Please try again later."
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
        reply = "AI could not complete this request. Please try again later."
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

        if method == "POST" and path == "/migrate-practice-stats":
            return handle_migrate_practice_stats(event)

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
