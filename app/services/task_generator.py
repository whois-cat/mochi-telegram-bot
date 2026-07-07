from __future__ import annotations

import html
from typing import Any

from app.config import (
    TASK_TYPE_FILL_BLANK,
    TASK_TYPE_OWN_SENTENCE,
    TASK_TYPE_TRANSLATE_RU_EN,
)


def build_task_prompt(task: dict[str, Any], task_number: int, total_tasks: int) -> str:
    task_type = task.get("task_type")
    title_by_type = {
        TASK_TYPE_FILL_BLANK: "Fill in the blank:",
        TASK_TYPE_TRANSLATE_RU_EN: "Translation:",
        TASK_TYPE_OWN_SENTENCE: "Own sentence:",
    }
    title = title_by_type.get(str(task_type), "Practice:")

    return "\n".join(
        [
            f"Task {task_number}/{total_tasks}",
            title,
            "",
            html.escape(str(task.get("prompt") or task.get("prompt_text") or "")),
        ]
    )
