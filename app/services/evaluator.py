from __future__ import annotations

import difflib
from typing import Any

from app.config import (
    ANSWER_FUZZY_MATCH_THRESHOLD,
    RESULT_CORRECT,
    RESULT_MINOR_ISSUE,
    RESULT_WRONG,
)
from app.services.normalization import (
    contains_target_phrase,
    matches_any_accepted,
    normalize_text,
)


def accepted_answers_for(task: dict[str, Any]) -> list[str]:
    answers = []
    expected = str(task.get("expected_answer") or "")
    if expected:
        answers.append(expected)

    for answer in task.get("accepted_answers") or []:
        answer = str(answer or "")
        if answer and answer not in answers:
            answers.append(answer)

    return answers


def evaluate_short_answer_locally(task: dict[str, Any], user_answer: str) -> dict[str, str]:
    accepted_answers = accepted_answers_for(task)
    if not user_answer.strip() or not accepted_answers:
        return {
            "result": RESULT_WRONG,
            "feedback": f"Wrong. Correct: {task.get('expected_answer')}",
        }

    if matches_any_accepted(user_answer, accepted_answers):
        return {"result": RESULT_CORRECT, "feedback": "Correct."}

    answer_norm = normalize_text(user_answer)
    if any(
        difflib.SequenceMatcher(None, answer_norm, normalize_text(expected)).ratio()
        >= ANSWER_FUZZY_MATCH_THRESHOLD
        for expected in accepted_answers
    ):
        return {
            "result": RESULT_MINOR_ISSUE,
            "feedback": f"Minor issue. Expected: {task.get('expected_answer')}",
        }

    return {
        "result": RESULT_WRONG,
        "feedback": f"Wrong. Correct: {task.get('expected_answer')}",
    }


def precheck_translation(task: dict[str, Any], user_answer: str) -> dict[str, str] | None:
    if matches_any_accepted(user_answer, accepted_answers_for(task)):
        return {"result": RESULT_CORRECT, "feedback": "Correct."}

    return None


def precheck_own_sentence(task: dict[str, Any], user_answer: str) -> dict[str, str] | None:
    target = str(task.get("word") or task.get("expected_answer") or "")
    if target and not contains_target_phrase(user_answer, target):
        return {
            "result": RESULT_WRONG,
            "feedback": f'Please use "{target}" in your sentence.',
        }

    return None


def format_own_sentence_feedback(
    *,
    word: str,
    result: str,
    feedback: str,
    target_word_sentence: str,
    natural_alternative: str = "",
) -> str:
    if result == RESULT_CORRECT:
        return feedback or "Good sentence."

    lines = []
    if feedback:
        lines.append(feedback)

    if target_word_sentence:
        lines.append(f'Better with "{word}": {target_word_sentence}')
    else:
        lines.append(f'Better with "{word}": Try a sentence where "{word}" fits naturally.')

    if natural_alternative:
        lines.append(f"Natural alternative: {natural_alternative}")

    return "\n".join(lines)
