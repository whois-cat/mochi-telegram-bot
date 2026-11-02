from __future__ import annotations

import json
import time
import unittest
from unittest.mock import patch

from botocore.exceptions import ClientError

from app import main
from app.config import get_gemini_models

CONFIG = {"GEMINI_API_KEY": "key", "GEMINI_MODEL": "model-a, model-b,model-c"}

CARD_JSON = json.dumps(
    {
        "word": "reliable",
        "translation": "надёжный",
        "usage": "adjective",
        "example": "She is reliable.",
        "frequency": "common",
        "level": "B1",
    }
)


class GeminiError(Exception):
    """Stands in for google.genai APIError: only `code` matters to the app."""

    def __init__(self, code: int | None):
        self.code = code
        super().__init__(code)


def fake_gemini(answers: dict[str, object]):
    """request_gemini_text replacement: per model a text answer or an exception to raise."""
    calls: list[tuple[str, int]] = []

    def request(prompt, *, model, config, timeout_ms):
        calls.append((model, timeout_ms))
        answer = answers[model]
        if isinstance(answer, Exception):
            raise answer
        return answer

    return request, calls


class GeminiFallbackTests(unittest.TestCase):
    def setUp(self):
        main._gemini_cooldowns.clear()
        self.addCleanup(main._gemini_cooldowns.clear)

    def generate(self, answers, *, deadline=None):
        request, calls = fake_gemini(answers)
        with patch.object(main, "request_gemini_text", side_effect=request), \
                patch.object(main, "load_prompt", return_value="{word}"):
            try:
                return main.generate_card_with_ai("reliable", CONFIG, deadline=deadline), calls
            except main.AiGenerationError:
                return None, calls

    def test_model_list_is_parsed_from_gemini_model(self):
        self.assertEqual(get_gemini_models(CONFIG), ["model-a", "model-b", "model-c"])
        self.assertEqual(get_gemini_models({"GEMINI_MODEL": "solo"}), ["solo"])

    def test_first_model_answers(self):
        card, calls = self.generate({"model-a": CARD_JSON})
        self.assertEqual(card.translation, "надёжный")
        self.assertEqual([model for model, _ in calls], ["model-a"])

    def test_overloaded_model_falls_back_to_next_and_cools_down(self):
        card, calls = self.generate({"model-a": GeminiError(503), "model-b": CARD_JSON})
        self.assertEqual(card.translation, "надёжный")
        self.assertEqual([model for model, _ in calls], ["model-a", "model-b"])
        self.assertTrue(main.is_model_cooling_down("model-a"))

        # The next word skips the cooling model and goes straight to model-b.
        _, calls = self.generate({"model-a": GeminiError(503), "model-b": CARD_JSON})
        self.assertEqual([model for model, _ in calls], ["model-b"])

    def test_quota_and_timeout_fall_back_too(self):
        card, calls = self.generate(
            {"model-a": GeminiError(429), "model-b": TimeoutError(), "model-c": CARD_JSON}
        )
        self.assertIsNotNone(card)
        self.assertEqual([model for model, _ in calls], ["model-a", "model-b", "model-c"])

    def test_invalid_json_tries_next_model_without_cooldown(self):
        card, _ = self.generate({"model-a": "not json", "model-b": CARD_JSON})
        self.assertIsNotNone(card)
        self.assertFalse(main.is_model_cooling_down("model-a"))

    def test_bad_key_stops_without_trying_other_models(self):
        card, calls = self.generate({"model-a": GeminiError(403), "model-b": CARD_JSON})
        self.assertIsNone(card)
        self.assertEqual([model for model, _ in calls], ["model-a"])

    def test_all_models_failing_raises(self):
        card, calls = self.generate({m: GeminiError(503) for m in ("model-a", "model-b", "model-c")})
        self.assertIsNone(card)
        self.assertEqual(len(calls), 3)

    def test_all_models_cooling_down_are_still_tried(self):
        for model in ("model-a", "model-b", "model-c"):
            main.start_model_cooldown(model)
        card, calls = self.generate({"model-a": CARD_JSON})
        self.assertIsNotNone(card)
        self.assertEqual([model for model, _ in calls], ["model-a"])

    def test_attempt_timeout_never_passes_the_deadline(self):
        _, calls = self.generate({"model-a": CARD_JSON}, deadline=time.monotonic() + 3)
        self.assertLessEqual(calls[0][1], 3000)

        _, calls = self.generate({"model-a": CARD_JSON}, deadline=time.monotonic() + 60)
        self.assertEqual(calls[0][1], main.GEMINI_ATTEMPT_TIMEOUT_MS)

    def test_no_call_when_deadline_is_too_close(self):
        card, calls = self.generate({"model-a": CARD_JSON}, deadline=time.monotonic() + 0.5)
        self.assertIsNone(card)
        self.assertEqual(calls, [])

    def test_timeout_on_shortened_attempt_does_not_cool_down(self):
        self.generate({"model-a": TimeoutError(), "model-b": CARD_JSON}, deadline=time.monotonic() + 3)
        self.assertFalse(main.is_model_cooling_down("model-a"))


class BulkDeadlineTests(unittest.TestCase):
    def test_finished_words_are_answered_when_others_run_out_of_time(self):
        def fake_generate(word, config, *, deadline):
            if word == "slow":
                raise main.AiGenerationError("deadline")
            return main.CardInput(word=word, translation="перевод", usage="noun", example="Ex.")

        def fake_store(card, config, *, destination, source):
            return main.AddCardResult(created=False, word=card.word, status=main.STATUS_DRAFT,
                                      translation=card.translation)

        with patch.object(main, "get_known_word", return_value=None), \
                patch.object(main, "generate_card_with_ai", side_effect=fake_generate), \
                patch.object(main, "store_new_card", side_effect=fake_store):
            results = main.add_ai_cards(["fast", "slow"], {}, destination=None)

        self.assertEqual(results[0][1].translation, "перевод")
        self.assertIsNone(results[1][1])
        texts = [text for text, _ in main.build_word_replies(results)]
        self.assertIn("❌ <b>slow</b> — failed, try again", texts)
        self.assertTrue(any("перевод" in text for text in texts))


class FakeTable:
    def __init__(self, error_code: str | None = None):
        self.items: dict[str, dict] = {}
        self.error_code = error_code

    def put_item(self, *, Item, ConditionExpression):
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code}}, "PutItem")
        key = Item["telegram_user_id"]
        if key in self.items:
            raise ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = Item


class TelegramUpdateDedupTests(unittest.TestCase):
    def webhook(self, table, update):
        event = {"body": json.dumps(update), "headers": {}}
        with patch.object(main, "get_sessions_table", return_value=table), \
                patch.object(main, "get_app_config", return_value={"TELEGRAM_WEBHOOK_SECRET": "s"}), \
                patch.object(main, "require_secret_header"), \
                patch.object(main, "process_telegram_text", return_value=[("ok", None)]) as process, \
                patch.object(main, "send_telegram_replies"):
            response = main.handle_telegram_webhook(event)
        return response, process

    def test_retried_update_is_processed_once(self):
        table = FakeTable()
        update = {"update_id": 42, "message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "/ai word"}}

        _, process = self.webhook(table, update)
        process.assert_called_once()
        self.assertGreater(table.items["update#42"]["expires_at"], time.time())

        response, process = self.webhook(table, update)
        process.assert_not_called()
        self.assertEqual(response["statusCode"], 200)

    def test_dynamodb_error_does_not_silence_the_bot(self):
        update = {"update_id": 7, "message": {"chat": {"id": 1}, "from": {"id": 1}, "text": "/ai word"}}
        _, process = self.webhook(FakeTable(error_code="ProvisionedThroughputExceededException"), update)
        process.assert_called_once()


if __name__ == "__main__":
    unittest.main()
