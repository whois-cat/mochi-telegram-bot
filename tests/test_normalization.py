from __future__ import annotations

import unittest

from app.services.normalization import (
    are_equivalent,
    contains_target_phrase,
    get_normalized_variants,
    normalize_text,
)


class NormalizationTests(unittest.TestCase):
    def test_normalize_text_cleans_case_spacing_and_punctuation(self):
        self.assertEqual(normalize_text("I’m in the middle of."), "i'm in the middle of")
        self.assertEqual(normalize_text("  Do NOT worry! "), "do not worry")

    def test_contractions_and_full_forms_are_equivalent(self):
        self.assertTrue(are_equivalent("I am in the middle of", "I'm in the middle of"))
        self.assertTrue(are_equivalent("Do not worry.", "don't worry"))
        self.assertTrue(are_equivalent("Cannot wait!", "can't wait"))

    def test_ambiguous_contractions_generate_multiple_variants(self):
        variants = get_normalized_variants("He's already left")
        self.assertIn("he is already left", variants)
        self.assertIn("he has already left", variants)

    def test_contains_target_phrase_uses_variants(self):
        self.assertTrue(contains_target_phrase("I am in the middle of work.", "I'm in the middle of"))


if __name__ == "__main__":
    unittest.main()
