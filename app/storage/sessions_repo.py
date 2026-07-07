from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from app.config import (
    PRACTICE_HISTORY_TTL_SECONDS,
    RESULT_CORRECT,
    RESULT_IDK,
    RESULT_MINOR_ISSUE,
    RESULT_SKIPPED,
    RESULT_WRONG,
    SHORT_FEEDBACK_MAX_CHARS,
)

SESSION_ENTITY_TYPE = "practice_session"
TASK_ENTITY_TYPE = "practice_task"
COMPACT_STORAGE_VERSION = 2


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def history_expires_at() -> int:
    return int(time.time()) + PRACTICE_HISTORY_TTL_SECONDS


def task_item_key(user_id: str, session_id: str, task_index: int) -> str:
    return f"{user_id}#session#{session_id}#task#{task_index:03d}"


def truncate_feedback(feedback: str, max_chars: int = SHORT_FEEDBACK_MAX_CHARS) -> str:
    if len(feedback) <= max_chars:
        return feedback
    return feedback[: max_chars - 1].rstrip() + "…"


def compact_task_item(
    *,
    user_id: str,
    session_id: str,
    task_index: int,
    task: dict[str, Any],
    expires_at: int,
) -> dict[str, Any]:
    target_word_keys = task.get("target_word_keys") or task.get("linked_word_keys") or []
    item = {
        "telegram_user_id": task_item_key(user_id, session_id, task_index),
        "entity_type": TASK_ENTITY_TYPE,
        "storage_version": COMPACT_STORAGE_VERSION,
        "session_id": session_id,
        "user_id": user_id,
        "task_index": task_index,
        "task_type": task.get("task_type"),
        "prompt_text": task.get("prompt"),
        "prompt": task.get("prompt"),
        "target_word_keys": target_word_keys,
        "linked_word_keys": target_word_keys,
        "word": task.get("word"),
        "translation": task.get("translation"),
        "expected_answer": task.get("expected_answer"),
        "accepted_answers": task.get("accepted_answers") or [],
        "created_at": utc_now_iso(),
        "expires_at": expires_at,
    }

    # Keep compact but preserve small task-specific fields needed at runtime.
    for field_name in ("russian_sentence", "source_sentence", "support_words", "example"):
        if task.get(field_name):
            item[field_name] = task[field_name]

    return item


def save_practice_session(
    table: Any,
    *,
    user_id: str,
    session: dict[str, Any],
    tasks: list[dict[str, Any]],
) -> None:
    session_id = str(session["training_id"])
    expires_at = history_expires_at()
    now = utc_now_iso()

    metadata = {
        "telegram_user_id": user_id,
        "entity_type": SESSION_ENTITY_TYPE,
        "storage_version": COMPACT_STORAGE_VERSION,
        "session_id": session_id,
        "user_id": user_id,
        "session_type": session.get("session_type"),
        "mode": session.get("mode"),
        "status": session.get("status"),
        "created_at": session.get("created_at") or now,
        "updated_at": now,
        "completed_at": session.get("completed_at"),
        "current_task_index": int(session.get("current_task_index") or 0),
        "total_tasks": len(tasks),
        "correct_count": 0,
        "minor_issue_count": 0,
        "wrong_count": 0,
        "idk_count": 0,
        "skipped_count": 0,
        "expires_at": expires_at,
    }
    table.put_item(Item=metadata)

    for task_index, task in enumerate(tasks):
        table.put_item(
            Item=compact_task_item(
                user_id=user_id,
                session_id=session_id,
                task_index=task_index,
                task=task,
                expires_at=expires_at,
            )
        )


def get_task(table: Any, *, user_id: str, session_id: str, task_index: int) -> dict[str, Any] | None:
    return (
        table.get_item(
            Key={"telegram_user_id": task_item_key(user_id, session_id, task_index)},
            ConsistentRead=True,
        ).get("Item")
    )


def get_tasks(table: Any, *, user_id: str, session_id: str, total_tasks: int) -> list[dict[str, Any]]:
    tasks = []
    for task_index in range(total_tasks):
        task = get_task(table, user_id=user_id, session_id=session_id, task_index=task_index)
        if task:
            tasks.append(task)
    return tasks


def result_counter_name(result: str) -> str | None:
    return {
        RESULT_CORRECT: "correct_count",
        RESULT_MINOR_ISSUE: "minor_issue_count",
        RESULT_WRONG: "wrong_count",
        RESULT_IDK: "idk_count",
        RESULT_SKIPPED: "skipped_count",
    }.get(result)


def save_task_result(
    table: Any,
    *,
    user_id: str,
    session_id: str,
    task_index: int,
    user_answer: str,
    evaluation: dict[str, str],
) -> None:
    now = utc_now_iso()
    table.update_item(
        Key={"telegram_user_id": task_item_key(user_id, session_id, task_index)},
        UpdateExpression=(
            "SET user_answer = :answer, evaluation_result = :result, "
            "short_feedback = :feedback, answered_at = :answered_at"
        ),
        ExpressionAttributeValues={
            ":answer": user_answer,
            ":result": evaluation["result"],
            ":feedback": truncate_feedback(evaluation.get("feedback") or ""),
            ":answered_at": now,
        },
    )


def advance_session(
    table: Any,
    *,
    user_id: str,
    next_task_index: int,
    result: str,
    completed: bool,
) -> None:
    now = utc_now_iso()
    set_parts = [
        "current_task_index = :next_task_index",
        "updated_at = :now",
    ]
    expression_values = {
        ":next_task_index": next_task_index,
        ":now": now,
        ":one": 1,
        ":zero": 0,
    }

    counter_name = result_counter_name(result)
    if counter_name:
        set_parts.append(f"{counter_name} = if_not_exists({counter_name}, :zero) + :one")

    if completed:
        set_parts.extend(["#status = :completed", "completed_at = :now"])
        expression_values[":completed"] = "completed"

    update_kwargs = {
        "Key": {"telegram_user_id": user_id},
        "UpdateExpression": "SET " + ", ".join(set_parts),
        "ExpressionAttributeValues": expression_values,
    }
    if completed:
        update_kwargs["ExpressionAttributeNames"] = {"#status": "status"}

    table.update_item(**update_kwargs)


def mark_session_status(table: Any, *, user_id: str, status: str) -> None:
    table.update_item(
        Key={"telegram_user_id": user_id},
        UpdateExpression="SET #status = :status, updated_at = :now, completed_at = :now",
        ExpressionAttributeNames={"#status": "status"},
        ExpressionAttributeValues={":status": status, ":now": utc_now_iso()},
    )
