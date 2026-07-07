from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from app import config
from app import main


def make_word(index: int, **overrides):
    word = f"word{index}"
    item = {
        "word_key": f"deck#test#word#{word}",
        "status": main.STATUS_CREATED,
        "word": word,
        "translation": f"перевод {index}",
        "example": f"I need to {word} this sentence.",
        "cloze_sentence": f"I need to {word} this sentence.",
        "correct_count": 0,
        "wrong_count": 0,
        "minor_issue_count": 0,
        "practice_score": 0,
    }
    item.update(overrides)
    return item


class FakeTable:
    def __init__(self, item):
        self.item = item
        self.updated = None

    def get_item(self, **kwargs):
        return {"Item": self.item}

    def update_item(self, **kwargs):
        self.updated = kwargs


class PracticeTests(unittest.TestCase):
    def test_today_builds_three_blocks(self):
        words = [make_word(index) for index in range(35)]

        def fake_generate(selected_words, _config):
            return {
                item["word_key"]: {
                    "russian_sentence": f"Русское предложение {number}.",
                    "expected_english": f"English sentence with {item['word']}.",
                }
                for number, item in enumerate(selected_words, 1)
            }

        with (
            patch("app.main.get_known_words_for_practice", return_value=words),
            patch("app.main.generate_translation_tasks_with_gemini", side_effect=fake_generate),
            patch("app.main.random.random", return_value=0.0),
        ):
            tasks = main.build_today_practice_tasks({"GEMINI_MODEL": "test"})

        self.assertEqual(len(tasks), 30)
        self.assertEqual(
            [task["task_type"] for task in tasks[:10]],
            [config.TASK_TYPE_FILL_BLANK] * 10,
        )
        self.assertEqual(
            [task["task_type"] for task in tasks[10:20]],
            [config.TASK_TYPE_TRANSLATE_RU_EN] * 10,
        )
        self.assertEqual(
            [task["task_type"] for task in tasks[20:]],
            [config.TASK_TYPE_OWN_SENTENCE] * 10,
        )

    def test_candidate_scoring_prioritizes_problem_words(self):
        now = datetime.now(timezone.utc)
        strong = make_word(
            1,
            correct_count=8,
            last_practiced_at=now.isoformat(),
            next_practice_at=(now + timedelta(days=7)).isoformat(),
        )
        weak = make_word(
            2,
            wrong_count=3,
            minor_issue_count=1,
            practice_score=4,
            next_practice_at=(now - timedelta(days=2)).isoformat(),
        )

        with patch("app.main.random.random", return_value=0.0):
            chosen = main.choose_practice_block([strong, weak], 1)

        self.assertEqual(chosen[0]["word"], "word2")

    def test_practice_interval_progression(self):
        self.assertEqual(main.calculate_next_active_practice(config.RESULT_CORRECT, 3, 0), (2, 1))
        self.assertEqual(main.calculate_next_active_practice(config.RESULT_CORRECT, 0, 1), (0, 3))
        self.assertEqual(main.calculate_next_active_practice(config.RESULT_MINOR_ISSUE, 0, 14), (1, 3))
        self.assertEqual(main.calculate_next_active_practice(config.RESULT_WRONG, 8, 14), (10, 0))

    def test_update_word_practice_stats_sets_result_fields(self):
        table = FakeTable({"practice_score": 0, "practice_interval_days": 0})

        with patch("app.main.get_known_words_table", return_value=table):
            main.update_word_practice_stats("word-key", config.RESULT_CORRECT)

        self.assertIn("correct_count", table.updated["UpdateExpression"])
        self.assertIn("last_correct_at", table.updated["UpdateExpression"])
        self.assertEqual(table.updated["ExpressionAttributeValues"][":interval"], 1)

    def test_old_review_modes_are_not_in_ui(self):
        labels = [
            label
            for rows in (config.HELP_MENU_BUTTONS,)
            for row in rows
            for label, _callback_data in row
        ]
        commands = [command["command"] for command in config.TELEGRAM_BOT_COMMANDS]

        self.assertNotIn("EN -> RU", labels)
        self.assertNotIn("RU -> EN", labels)
        self.assertNotIn("EN → RU", labels)
        self.assertNotIn("RU → EN", labels)
        self.assertNotIn("Weak words", labels)
        self.assertNotIn("Due", labels)
        self.assertNotIn("practice", commands)
        self.assertNotIn("weak", commands)

        with self.assertRaises(main.UserInputError):
            main.parse_telegram_command("/practice")

        with self.assertRaises(main.UserInputError):
            main.parse_telegram_command("/weak")

    def test_migration_removes_legacy_srs_fields(self):
        item = {
            "word_key": "word-key",
            "word": "reliable",
            "correct_count": 2,
            "wrong_count": 1,
            "review_count": 3,
            "streak": 2,
            "interval_days": 7,
            "due_at": "2026-07-01T00:00:00+00:00",
        }

        migrated = main.migrate_legacy_practice_item(item, "2026-07-06T00:00:00+00:00")

        self.assertEqual(migrated["correct_count"], 2)
        self.assertEqual(migrated["wrong_count"], 1)
        self.assertEqual(migrated["practice_attempt_count"], 3)
        self.assertNotIn("review_count", migrated)
        self.assertNotIn("streak", migrated)
        self.assertNotIn("interval_days", migrated)
        self.assertNotIn("due_at", migrated)


if __name__ == "__main__":
    unittest.main()
