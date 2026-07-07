from __future__ import annotations

from typing import Any

from app.config import (
    CLOZE_BLANK,
    HINT_TEXT_EQUIVALENTS,
    IDK_TEXT_EQUIVALENTS,
    TASK_TYPE_FILL_BLANK,
    TASK_TYPE_OWN_SENTENCE,
    TASK_TYPE_TRANSLATE_RU_EN,
)
from app.services.normalization import normalize_text


def is_idk_text(text: str) -> bool:
    return normalize_text(text) in IDK_TEXT_EQUIVALENTS


def is_hint_text(text: str) -> bool:
    return normalize_text(text) in HINT_TEXT_EQUIVALENTS


def _word_hint(answer: str) -> str:
    clean = answer.strip()
    if not clean:
        return "Hint: think of the target word from this card."

    first = clean[0]
    if " " in clean:
        parts = clean.split()
        return f'Hint: {len(parts)} words, starts with "{first}".'

    prefix = clean[:2] if len(clean) >= 2 else first
    return f'Hint: {len(clean)} letters, starts with "{prefix}...".'


def _translation_hint(task: dict[str, Any]) -> str:
    word = str(task.get("word") or "").strip()
    expected = str(task.get("expected_answer") or "").strip()

    if word:
        return f'Hint: try to use "{word}".'

    if expected:
        preview = " ".join(expected.split()[:5])
        return f'Hint: try starting with "{preview}...".'

    return "Hint: translate the whole idea, not word by word."


def _own_sentence_hint(task: dict[str, Any]) -> str:
    word = str(task.get("word") or "").strip()
    translation = str(task.get("translation") or "").strip()
    example = str(task.get("example") or "").strip()

    if translation and word:
        return f'Hint: "{word}" means {translation}. Try using it in your own situation.'

    if example:
        starter = example.split(word, 1)[0].strip() if word and word in example else ""
        if starter:
            return f"Hint: try a sentence like: {starter}..."

    if word:
        return f'Hint: write a complete sentence that includes "{word}".'

    return "Hint: write a complete sentence with the target word."


def build_hint(task: dict[str, Any]) -> str:
    task_type = task.get("task_type")

    if task_type == TASK_TYPE_FILL_BLANK:
        return _word_hint(str(task.get("expected_answer") or ""))

    if task_type == TASK_TYPE_TRANSLATE_RU_EN:
        return _translation_hint(task)

    if task_type == TASK_TYPE_OWN_SENTENCE:
        return _own_sentence_hint(task)

    if task.get("prompt"):
        return f"Hint: look for the part around {CLOZE_BLANK}."

    return "Hint: use the target word from this task."
