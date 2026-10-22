from __future__ import annotations

import unittest
from unittest.mock import patch

from app import main


def make_card(word: str) -> main.CardInput:
    return main.CardInput(
        word=word,
        translation=f"перевод {word}",
        usage="phrase",
        example=f"I use {word} here.",
        frequency="common",
        level="B2",
    )


def make_item(word: str, status: str, seen_count: int = 1) -> dict:
    return {
        "word_key": f"deck#test#word#{word}",
        "word": word,
        "translation": f"перевод {word}",
        "status": status,
        "seen_count": seen_count,
    }


def buttons(keyboard: dict | None) -> list[str]:
    return [button["text"] for row in (keyboard or {}).get("inline_keyboard", []) for button in row]


class SplitBulkWordsTests(unittest.TestCase):
    def test_splits_lines_and_semicolons(self):
        self.assertEqual(
            main.split_bulk_words("reliable\ntake for granted; so far this month"),
            ["reliable", "take for granted", "so far this month"],
        )

    def test_strips_list_markers_blanks_and_duplicates(self):
        self.assertEqual(
            main.split_bulk_words("- reliable\n\n2) Reliable\n• well-known\n3. so far"),
            ["reliable", "well-known", "so far"],
        )


class ParseCommandTests(unittest.TestCase):
    def test_ai_mochi_inbox_set_destination(self):
        self.assertIsNone(main.parse_telegram_command("/ai reliable").destination)
        self.assertEqual(main.parse_telegram_command("/mochi reliable").destination, main.DESTINATION_MOCHI)
        self.assertEqual(main.parse_telegram_command("/inbox reliable").destination, main.DESTINATION_INBOX)

    def test_bare_inbox_lists_inbox(self):
        self.assertEqual(main.parse_telegram_command("/inbox").mode, "inbox_list")

    def test_newline_right_after_command_is_a_list(self):
        command = main.parse_telegram_command("/mochi\nreliable\nso far this month")
        self.assertEqual(command.mode, "ai")
        self.assertEqual(command.queries, ("reliable", "so far this month"))

    def test_too_many_words_is_rejected(self):
        text = "/ai " + "; ".join(f"word{i}" for i in range(main.BULK_AI_MAX_WORDS + 1))
        with self.assertRaises(main.UserInputError):
            main.parse_telegram_command(text)

    def test_ai_without_words_is_rejected(self):
        with self.assertRaises(main.UserInputError):
            main.parse_telegram_command("/ai")


class AddAiCardsTests(unittest.TestCase):
    def test_existing_words_skip_gemini_and_failures_do_not_stop_the_list(self):
        existing = make_item("reliable", main.STATUS_INBOX)

        def fake_generate(word, config):
            if word == "broken":
                raise main.AiGenerationError("boom")
            return make_card(word)

        def fake_store(card, config, *, destination, source):
            return main.AddCardResult(created=True, word=card.word, status=main.STATUS_DRAFT)

        with patch.object(main, "get_known_word", side_effect=lambda w, c: existing if w == "reliable" else None), \
                patch.object(main, "handle_existing_word", return_value=main.AddCardResult(
                    created=False, word="reliable", status=main.STATUS_INBOX, seen_count=2)) as handle_existing, \
                patch.object(main, "generate_card_with_ai", side_effect=fake_generate) as generate, \
                patch.object(main, "store_new_card", side_effect=fake_store):
            results = main.add_ai_cards(["reliable", "so far", "broken"], {}, destination=None)

        self.assertEqual([word for word, _ in results], ["reliable", "so far", "broken"])
        self.assertEqual(results[0][1].seen_count, 2)
        self.assertEqual(results[1][1].status, main.STATUS_DRAFT)
        self.assertIsNone(results[2][1])
        handle_existing.assert_called_once_with(existing, None, {})
        self.assertEqual(sorted(call.args[0] for call in generate.call_args_list), ["broken", "so far"])

        replies = main.build_word_replies(results)
        # The failure is one summary line; the inbox word and the draft keep their cards.
        self.assertEqual(len(replies), 3)
        self.assertEqual(replies[0], ("✗ <b>broken</b> — failed, try again", None))


class HandleExistingWordTests(unittest.TestCase):
    def test_word_in_mochi_is_not_counted(self):
        item = make_item("reliable", main.STATUS_CREATED)
        with patch.object(main, "record_sighting") as record:
            result = main.handle_existing_word(item, main.DESTINATION_INBOX, {})
        record.assert_not_called()
        self.assertEqual(result.status, main.STATUS_CREATED)
        self.assertFalse(result.created)

    def test_inbox_word_is_counted_and_sent_to_mochi_on_request(self):
        item = make_item("reliable", main.STATUS_INBOX)
        sent = main.AddCardResult(created=True, word="reliable", status=main.STATUS_CREATED)
        with patch.object(main, "record_sighting", return_value={**item, "seen_count": 2}) as record, \
                patch.object(main, "send_known_word_to_mochi", return_value=sent) as send:
            result = main.handle_existing_word(item, main.DESTINATION_MOCHI, {})
        record.assert_called_once_with(item["word_key"])
        send.assert_called_once_with(item["word_key"], {})
        self.assertIs(result, sent)

    def test_draft_readded_without_destination_stays_a_draft(self):
        item = make_item("reliable", main.STATUS_DRAFT)
        with patch.object(main, "record_sighting", return_value={**item, "seen_count": 3}):
            result = main.handle_existing_word(item, None, {})
        self.assertEqual(result.status, main.STATUS_DRAFT)
        self.assertEqual(result.seen_count, 3)


class CardReplyTests(unittest.TestCase):
    def test_draft_card_is_plain_with_mochi_and_inbox_buttons(self):
        text, keyboard = main.build_add_result_reply(
            main.AddCardResult(
                created=True,
                word="infuse",
                status=main.STATUS_DRAFT,
                translation="наполнять",
                example="She infuses her tea.",
                frequency="moderate",
                level="C1",
            )
        )
        self.assertEqual(
            text,
            "<b>infuse</b> — наполнять\n<blockquote>She infuses her tea.</blockquote>\n<i>moderate · C1</i>",
        )
        self.assertIn(main.TO_MOCHI_BUTTON_TEXT, buttons(keyboard))
        self.assertIn(main.TO_INBOX_BUTTON_TEXT, buttons(keyboard))

    def test_just_sent_word_is_a_single_line_without_buttons(self):
        for status, destination in ((main.STATUS_CREATED, "Mochi"), (main.STATUS_INBOX, "Inbox")):
            reply = main.build_add_result_reply(main.AddCardResult(created=True, word="beneath", status=status))
            self.assertEqual(reply, (f"✓ <b>beneath</b> → {destination}", None))

    def test_several_sent_words_share_one_message(self):
        results = [
            (word, main.AddCardResult(created=True, word=word, status=main.STATUS_CREATED))
            for word in ("beneath", "infuse")
        ]
        self.assertEqual(
            main.build_word_replies(results),
            [("✓ <b>beneath</b> → Mochi\n✓ <b>infuse</b> → Mochi", None)],
        )

    def test_inbox_word_seen_often_suggests_mochi(self):
        text, keyboard = main.build_add_result_reply(
            main.AddCardResult(
                created=False, word="reliable", status=main.STATUS_INBOX, seen_count=main.INBOX_SUGGEST_SEEN_COUNT
            )
        )
        self.assertIn(main.INBOX_SUGGEST_TEXT, text)
        self.assertIn(f"in inbox · looked up {main.INBOX_SUGGEST_SEEN_COUNT} times", text)
        self.assertIn(main.TO_MOCHI_BUTTON_TEXT, buttons(keyboard))
        self.assertNotIn(main.TO_INBOX_BUTTON_TEXT, buttons(keyboard))

    def test_word_already_in_mochi_shows_card_with_card_actions(self):
        text, keyboard = main.build_add_result_reply(
            main.AddCardResult(created=False, word="reliable", status=main.STATUS_CREATED)
        )
        self.assertIn("in Mochi", text)
        self.assertEqual(buttons(keyboard), [main.REGENERATE_BUTTON_TEXT, main.EDIT_BUTTON_TEXT, main.DELETE_BUTTON_TEXT])

    def test_replies_have_no_decorative_emoji(self):
        texts = [
            main.TO_MOCHI_BUTTON_TEXT,
            main.TO_INBOX_BUTTON_TEXT,
            main.INBOX_PROMOTE_BUTTON_TEXT,
            *main.WORD_STATUS_LABELS.values(),
        ]
        for text in texts:
            self.assertTrue(all(ord(char) < 0x2000 for char in text), text)


class InboxTests(unittest.TestCase):
    def test_list_shows_words_and_promote_button(self):
        items = [make_item("reliable", main.STATUS_INBOX, 3), make_item("so far", main.STATUS_INBOX)]
        with patch.object(main, "scan_inbox_items", return_value=items):
            text, keyboard = main.build_inbox_reply({})
        self.assertIn("<b>Inbox</b> · 2", text)
        self.assertIn("looked up 3 times", text)
        self.assertNotIn("in inbox", text)
        self.assertIn("reliable → Mochi", buttons(keyboard))
        self.assertEqual(keyboard["inline_keyboard"][-1][0]["callback_data"], main.CALLBACK_INBOX_PROMOTE)

    def test_promote_sends_only_words_seen_enough(self):
        items = [make_item("reliable", main.STATUS_INBOX, 2), make_item("so far", main.STATUS_INBOX, 1)]
        with patch.object(main, "scan_inbox_items", return_value=items), \
                patch.object(main, "send_known_word_to_mochi", return_value=main.AddCardResult(
                    created=True, word="reliable", status=main.STATUS_CREATED)) as send:
            results = main.promote_seen_inbox_words({})
        send.assert_called_once_with("deck#test#word#reliable", {})
        self.assertEqual([word for word, _ in results], ["reliable"])


if __name__ == "__main__":
    unittest.main()
