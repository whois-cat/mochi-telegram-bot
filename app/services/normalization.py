from __future__ import annotations

import re
import unicodedata


TRAILING_PUNCTUATION_RE = re.compile(r"[\s.!?;:,]+$")
SPACING_RE = re.compile(r"\s+")

CONTRACTION_EXPANSIONS: dict[str, tuple[str, ...]] = {
    "i'm": ("i am",),
    "you're": ("you are",),
    "he's": ("he is", "he has"),
    "she's": ("she is", "she has"),
    "it's": ("it is", "it has"),
    "we're": ("we are",),
    "they're": ("they are",),
    "i've": ("i have",),
    "you've": ("you have",),
    "we've": ("we have",),
    "they've": ("they have",),
    "i'll": ("i will",),
    "you'll": ("you will",),
    "he'll": ("he will",),
    "she'll": ("she will",),
    "we'll": ("we will",),
    "they'll": ("they will",),
    "i'd": ("i would", "i had"),
    "you'd": ("you would", "you had"),
    "he'd": ("he would", "he had"),
    "she'd": ("she would", "she had"),
    "we'd": ("we would", "we had"),
    "they'd": ("they would", "they had"),
    "don't": ("do not",),
    "doesn't": ("does not",),
    "didn't": ("did not",),
    "can't": ("cannot",),
    "won't": ("will not",),
    "isn't": ("is not",),
    "aren't": ("are not",),
    "wasn't": ("was not",),
    "weren't": ("were not",),
    "haven't": ("have not",),
    "hasn't": ("has not",),
    "hadn't": ("had not",),
    "shouldn't": ("should not",),
    "wouldn't": ("would not",),
    "couldn't": ("could not",),
}

FULL_FORM_CONTRACTIONS: dict[str, tuple[str, ...]] = {}
for contraction, expansions in CONTRACTION_EXPANSIONS.items():
    for expansion in expansions:
        FULL_FORM_CONTRACTIONS.setdefault(expansion, tuple())
        FULL_FORM_CONTRACTIONS[expansion] = (*FULL_FORM_CONTRACTIONS[expansion], contraction)


def normalize_text(text: str) -> str:
    value = unicodedata.normalize("NFKC", text)
    value = (
        value.replace("’", "'")
        .replace("‘", "'")
        .replace("`", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("—", "-")
        .replace("–", "-")
    )
    value = value.lower().strip()
    value = TRAILING_PUNCTUATION_RE.sub("", value)
    value = re.sub(r"\s+([,.!?;:])", r"\1", value)
    value = SPACING_RE.sub(" ", value)
    return value.strip()


def _replace_phrase_once(text: str, phrase: str, replacement: str) -> str | None:
    pattern = rf"(?<!\w){re.escape(phrase)}(?!\w)"
    replaced, count = re.subn(pattern, replacement, text, count=1)
    return replaced if count else None


def get_normalized_variants(text: str) -> set[str]:
    base = normalize_text(text)
    if not base:
        return set()

    variants = {base}
    changed = True

    # Generate a small equivalence closure. Ambiguous forms intentionally fan
    # out because these variants are used only for matching.
    while changed and len(variants) < 128:
        changed = False
        for current in list(variants):
            replacement_groups = (
                CONTRACTION_EXPANSIONS.items(),
                FULL_FORM_CONTRACTIONS.items(),
            )
            for replacements in replacement_groups:
                for source, targets in replacements:
                    for target in targets:
                        replaced = _replace_phrase_once(current, source, target)
                        if replaced and replaced not in variants:
                            variants.add(normalize_text(replaced))
                            changed = True
                            if len(variants) >= 128:
                                return variants

    return variants


def are_equivalent(user_answer: str, expected_answer: str) -> bool:
    return bool(get_normalized_variants(user_answer) & get_normalized_variants(expected_answer))


def matches_any_accepted(user_answer: str, accepted_answers: list[str]) -> bool:
    return any(are_equivalent(user_answer, accepted) for accepted in accepted_answers if accepted)


def contains_target_phrase(user_answer: str, target_phrase: str) -> bool:
    answer_variants = get_normalized_variants(user_answer)
    target_variants = get_normalized_variants(target_phrase)

    for answer in answer_variants:
        for target in target_variants:
            if re.search(rf"(?<!\w){re.escape(target)}(?!\w)", answer):
                return True

    return False
