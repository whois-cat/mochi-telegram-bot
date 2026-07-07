from __future__ import annotations

import unittest
from unittest.mock import patch

from app import config
from app import main


def make_task(**overrides):
    task = {
        "task_type": config.TASK_TYPE_TRANSLATE_RU_EN,
        "target_word_keys": ["word-key"],
        "word": "in the middle of",
        "translation": "в процессе",
        "prompt": "Переведи на английский:\nЯ сейчас в процессе работы.",
        "expected_answer": "I'm in the middle of work.",
        "accepted_answers": ["I'm in the middle of work."],
    }
    task.update(overrides)
    return task


class ActivePracticeBehaviorTests(unittest.TestCase):
    def test_accepted_variant_is_correct_without_gemini(self):
        task = make_task()

        with patch("app.main.evaluate_translation_with_gemini", side_effect=AssertionError):
            result = main.evaluate_training_task(task, "I am in the middle of work", {})

        self.assertEqual(result["result"], config.RESULT_CORRECT)

    def test_own_sentence_missing_target_does_not_call_gemini(self):
        task = make_task(task_type=config.TASK_TYPE_OWN_SENTENCE, word="unravel")

        with patch("app.main.evaluate_sentence_with_gemini", side_effect=AssertionError):
            result = main.evaluate_training_task(task, "We solved several riddles.", {})

        self.assertEqual(result["result"], config.RESULT_WRONG)
        self.assertIn('Please use "unravel"', result["feedback"])

    def test_own_sentence_feedback_keeps_target_word(self):
        task = make_task(task_type=config.TASK_TYPE_OWN_SENTENCE, word="unravel")

        with patch(
            "app.main.evaluate_sentence_with_gemini",
            return_value={
                "result": "minor_issue",
                "feedback": '"Unravel" works better with mysteries or complex problems.',
                "better_sentence": "We bought a mystery game where we have to unravel several clues.",
                "target_word_sentence": "We bought a mystery game where we have to unravel several clues.",
                "natural_alternative": "We bought a new game where we have to solve different riddles.",
            },
        ):
            result = main.evaluate_training_task(
                task,
                "We bought a new game where we have to unravel different riddles.",
                {},
            )

        self.assertEqual(result["result"], config.RESULT_MINOR_ISSUE)
        self.assertIn('Better with "unravel"', result["feedback"])
        self.assertIn("Natural alternative:", result["feedback"])
        self.assertLess(
            result["feedback"].index('Better with "unravel"'),
            result["feedback"].index("Natural alternative:"),
        )

    def test_hint_keeps_same_task_and_does_not_update_stats(self):
        session = {
            "telegram_user_id": "user-1",
            "session_type": config.PRACTICE_SESSION_TYPE_ACTIVE,
            "tasks": [make_task(task_type=config.TASK_TYPE_FILL_BLANK, expected_answer="unravel")],
            "current_task_index": 0,
        }

        with patch("app.main.update_word_practice_stats", side_effect=AssertionError):
            reply, markup = main.handle_practice_answer(session, "подсказка", {})

        self.assertIn("Hint:", reply)
        self.assertIn("Task 1/1", reply)
        self.assertIsNotNone(markup)

    def test_idk_moves_next_and_updates_stats(self):
        session = {
            "telegram_user_id": "user-1",
            "session_type": config.PRACTICE_SESSION_TYPE_ACTIVE,
            "tasks": [make_task(task_type=config.TASK_TYPE_FILL_BLANK, expected_answer="unravel")],
            "current_task_index": 0,
            "user_answers": [],
            "evaluation_results": [],
        }

        with (
            patch("app.main.update_word_practice_stats") as update_stats,
            patch("app.main.clear_session"),
        ):
            reply, markup = main.handle_practice_answer(session, "не помню", {})

        update_stats.assert_called_once_with("word-key", config.RESULT_IDK)
        self.assertIn("IDK.", reply)
        self.assertIn("Correct answer: unravel", reply)
        self.assertIsNone(markup)


if __name__ == "__main__":
    unittest.main()
