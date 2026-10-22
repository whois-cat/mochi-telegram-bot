from __future__ import annotations

import unittest

from app import config
from app import main


class RemovedCommandsTests(unittest.TestCase):
    def test_practice_commands_are_gone(self):
        commands = [command["command"] for command in config.TELEGRAM_BOT_COMMANDS]
        for removed in ("today", "stats", "practice", "weak"):
            self.assertNotIn(removed, commands)
            with self.assertRaises(main.UserInputError):
                main.parse_telegram_command(f"/{removed}")

    def test_edit_accepts_only_four_fields(self):
        card = main.parse_card_input_text("reliable | надежный | adjective | A reliable source.")
        self.assertEqual(card.word, "reliable")
        with self.assertRaises(main.UserInputError):
            main.parse_card_input_text("reliable | надежный | adjective | A reliable source. | extra")


if __name__ == "__main__":
    unittest.main()
