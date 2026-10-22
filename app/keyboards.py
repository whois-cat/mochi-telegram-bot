from __future__ import annotations

from typing import Any

from app.config import HELP_MENU_BUTTONS


def build_inline_keyboard(rows: tuple) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": label, "callback_data": data} for label, data in row] for row in rows
        ]
    }


def build_help_menu() -> dict[str, Any] | None:
    return build_inline_keyboard(HELP_MENU_BUTTONS) if HELP_MENU_BUTTONS else None
