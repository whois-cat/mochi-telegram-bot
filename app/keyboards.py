from __future__ import annotations

from typing import Any

from app.config import (
    CALLBACK_PRACTICE_HINT,
    CALLBACK_PRACTICE_IDK,
    HELP_MENU_BUTTONS,
    HINT_BUTTON_TEXT,
    IDK_BUTTON_TEXT,
)


def build_inline_keyboard(rows: tuple) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in rows
        ]
    }


def build_help_menu() -> dict[str, Any]:
    return build_inline_keyboard(HELP_MENU_BUTTONS)


def build_practice_task_keyboard() -> dict[str, Any]:
    return build_inline_keyboard(
        (
            (
                (IDK_BUTTON_TEXT, CALLBACK_PRACTICE_IDK),
                (HINT_BUTTON_TEXT, CALLBACK_PRACTICE_HINT),
            ),
        )
    )
