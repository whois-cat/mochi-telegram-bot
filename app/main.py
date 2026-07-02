import json
import os
from typing import Any

import requests
from pydantic import BaseModel, Field, ValidationError


MOCHI_API_URL = "https://app.mochi.cards/api"


class AddCardRequest(BaseModel):
    word: str = Field(min_length=1)
    translation: str = Field(min_length=1)
    usage: str = Field(min_length=1)
    example: str = Field(min_length=1)


def response(status_code: int, body: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
        },
        "body": json.dumps(body, ensure_ascii=False),
    }


def normalize_usage_tag(usage: str) -> str:
    return usage.lower().strip().replace(" ", "-")


def build_mochi_content(card: AddCardRequest) -> str:
    return (
        f"# {card.word}\n\n"
        f"**RU:** {card.translation}\n\n"
        f"**Usage:** {card.usage}\n\n"
        f"---\n\n"
        f"{card.example}"
    )


def create_mochi_card(card: AddCardRequest) -> str:
    mochi_api_key = os.environ["MOCHI_API_KEY"]
    mochi_deck_id = os.environ["MOCHI_DECK_ID"]

    usage_tag = normalize_usage_tag(card.usage)

    payload = {
        "deck-id": mochi_deck_id,
        "content": build_mochi_content(card),
        "manual-tags": ["english", "lambda", usage_tag],
    }

    api_response = requests.post(
        f"{MOCHI_API_URL}/cards/",
        auth=(mochi_api_key, ""),
        json=payload,
        timeout=20,
    )

    if not api_response.ok:
        print("Mochi status:", api_response.status_code)
        print("Mochi response:", api_response.text)

    api_response.raise_for_status()

    data = api_response.json()
    return data["id"]


def check_secret(event: dict[str, Any]) -> bool:
    expected_secret = os.getenv("APP_SECRET")

    if not expected_secret:
        return True

    headers = event.get("headers") or {}

    # AWS can pass headers with different casing.
    provided_secret = (
        headers.get("x-bot-secret")
        or headers.get("X-Bot-Secret")
    )

    return provided_secret == expected_secret


def parse_json_body(event: dict[str, Any]) -> dict[str, Any]:
    raw_body = event.get("body") or "{}"

    if event.get("isBase64Encoded"):
        raise ValueError("Base64 body is not supported yet")

    return json.loads(raw_body)


def handler(event, context):
    print("Event:", json.dumps(event, ensure_ascii=False))

    if not check_secret(event):
        return response(
            401,
            {
                "ok": False,
                "error": "Unauthorized",
            },
        )

    method = event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath", "/")

    if method != "POST":
        return response(
            405,
            {
                "ok": False,
                "error": "Method not allowed. Use POST.",
            },
        )

    if path not in {"/", "/add-card"}:
        return response(
            404,
            {
                "ok": False,
                "error": "Not found",
            },
        )

    try:
        body = parse_json_body(event)
        card = AddCardRequest.model_validate(body)
        mochi_card_id = create_mochi_card(card)

        return response(
            201,
            {
                "ok": True,
                "message": "Card added to Mochi",
                "mochi_card_id": mochi_card_id,
                "word": card.word,
            },
        )

    except ValidationError as error:
        return response(
            400,
            {
                "ok": False,
                "error": "Invalid request body",
                "details": error.errors(),
            },
        )

    except json.JSONDecodeError:
        return response(
            400,
            {
                "ok": False,
                "error": "Invalid JSON",
            },
        )

    except requests.HTTPError as error:
        return response(
            502,
            {
                "ok": False,
                "error": "Mochi API request failed",
                "details": str(error),
            },
        )

    except Exception as error:
        print("Unexpected error:", repr(error))

        return response(
            500,
            {
                "ok": False,
                "error": "Internal server error",
            },
        )