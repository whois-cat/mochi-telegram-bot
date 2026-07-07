from __future__ import annotations

import unittest

from app import config
from app.storage import sessions_repo


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, *, Item):
        self.items[Item["telegram_user_id"]] = dict(Item)

    def get_item(self, *, Key, ConsistentRead=False):
        return {"Item": self.items.get(Key["telegram_user_id"])}

    def update_item(self, *, Key, UpdateExpression, ExpressionAttributeValues, ExpressionAttributeNames=None):
        item = self.items.setdefault(Key["telegram_user_id"], {"telegram_user_id": Key["telegram_user_id"]})
        if "user_answer" in UpdateExpression:
            item["user_answer"] = ExpressionAttributeValues[":answer"]
            item["evaluation_result"] = ExpressionAttributeValues[":result"]
            item["short_feedback"] = ExpressionAttributeValues[":feedback"]
            item["answered_at"] = ExpressionAttributeValues[":answered_at"]
        if "current_task_index" in UpdateExpression:
            item["current_task_index"] = ExpressionAttributeValues[":next_task_index"]
            item["updated_at"] = ExpressionAttributeValues[":now"]
            for key in ("correct_count", "minor_issue_count", "wrong_count", "idk_count", "skipped_count"):
                if key in UpdateExpression:
                    item[key] = item.get(key, 0) + 1
            if ":completed" in ExpressionAttributeValues:
                item["status"] = ExpressionAttributeValues[":completed"]
                item["completed_at"] = ExpressionAttributeValues[":now"]


class SessionStorageTests(unittest.TestCase):
    def test_session_metadata_is_compact_and_tasks_are_separate(self):
        table = FakeTable()
        tasks = [
            {
                "task_type": config.TASK_TYPE_FILL_BLANK,
                "prompt": "_____ the clues.",
                "target_word_keys": ["word-key"],
                "expected_answer": "unravel",
                "accepted_answers": ["unravel"],
            }
        ]

        sessions_repo.save_practice_session(
            table,
            user_id="user-1",
            session={
                "training_id": "session-1",
                "session_type": config.PRACTICE_SESSION_TYPE_ACTIVE,
                "mode": config.PRACTICE_MODE_TODAY,
                "status": config.SESSION_STATUS_ACTIVE,
            },
            tasks=tasks,
        )

        metadata = table.items["user-1"]
        task_item = table.items[sessions_repo.task_item_key("user-1", "session-1", 0)]

        self.assertNotIn("tasks", metadata)
        self.assertNotIn("user_answers", metadata)
        self.assertNotIn("evaluation_results", metadata)
        self.assertEqual(metadata["total_tasks"], 1)
        self.assertEqual(task_item["task_type"], config.TASK_TYPE_FILL_BLANK)
        self.assertIn("expires_at", metadata)
        self.assertIn("expires_at", task_item)
        self.assertNotIn("raw_prompt", task_item)
        self.assertNotIn("raw_response", task_item)

    def test_feedback_is_truncated_before_storage(self):
        table = FakeTable()
        task_key = sessions_repo.task_item_key("user-1", "session-1", 0)
        table.items[task_key] = {"telegram_user_id": task_key}
        long_feedback = "x" * (config.SHORT_FEEDBACK_MAX_CHARS + 50)

        sessions_repo.save_task_result(
            table,
            user_id="user-1",
            session_id="session-1",
            task_index=0,
            user_answer="idk",
            evaluation={"result": config.RESULT_IDK, "feedback": long_feedback},
        )

        self.assertLessEqual(
            len(table.items[task_key]["short_feedback"]),
            config.SHORT_FEEDBACK_MAX_CHARS,
        )


if __name__ == "__main__":
    unittest.main()
