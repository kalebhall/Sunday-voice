"""Unit tests for profanity masking and hallucination cleanup."""

from __future__ import annotations

import pytest

from app.core.config import get_settings
from app.services.content_filter import (
    MASK,
    clean_transcript,
    for_speech,
    mask_profanity,
    strip_profanity,
)


class TestMaskProfanity:
    def test_masks_english_profanity(self) -> None:
        assert mask_profanity("what the fuck is that", "en") == f"what the {MASK} is that"

    def test_masks_inflections_and_case(self) -> None:
        assert mask_profanity("Fucking Bullshit!", "en") == f"{MASK} {MASK}!"

    def test_masks_language_specific_words(self) -> None:
        assert mask_profanity("eres un pendejo", "es") == f"eres un {MASK}"
        assert mask_profanity("putang ina mo", "tl") == f"{MASK} mo"

    def test_english_list_applies_to_every_language(self) -> None:
        assert mask_profanity("no shit hermano", "es") == f"no {MASK} hermano"

    def test_language_lists_do_not_leak_into_english(self) -> None:
        # "puke" is vulgar in Tagalog but ordinary in English.
        assert mask_profanity("the baby might puke", "en") == "the baby might puke"

    @pytest.mark.parametrize(
        "text",
        [
            "Ye shall make staves of shittim wood.",
            "He was saved from hell and damnation.",
            "Balaam's ass spoke to him.",
            "Before the cock crow, thou shalt deny me thrice.",
            "The Relief Society will meet in the classroom.",
            "Scunthorpe and Essex are places.",
        ],
    )
    def test_leaves_scripture_and_ordinary_words_alone(self, text: str) -> None:
        assert mask_profanity(text, "en") == text

    def test_extra_words_setting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONTENT_FILTER_EXTRA_WORDS", "darn, heck")
        get_settings.cache_clear()
        assert mask_profanity("oh heck, darn it", "en") == f"oh {MASK}, {MASK} it"

    def test_disabled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CONTENT_FILTER_ENABLED", "false")
        get_settings.cache_clear()
        assert mask_profanity("shit", "en") == "shit"
        assert clean_transcript("Thanks for watching!", "en") == "Thanks for watching!"


class TestStripAndSpeech:
    def test_strip_removes_words_and_masks(self) -> None:
        assert strip_profanity(f"this {MASK} is fucking great", "en") == "this is great"

    def test_for_speech_removes_masks(self) -> None:
        assert for_speech(f"hello {MASK}, friends") == "hello, friends"


class TestCleanTranscript:
    def test_removes_hallucinated_outro(self) -> None:
        text = "Let us close with a hymn. Thanks for watching!"
        assert clean_transcript(text, "en") == "Let us close with a hymn."

    @pytest.mark.parametrize(
        "text",
        [
            "Thank you for watching.",
            "Please subscribe to my channel.",
            "Subtitles by the Amara.org community",
            "Subtítulos realizados por la comunidad de Amara.org",
            "Gracias por ver el video.",
        ],
    )
    def test_pure_hallucination_becomes_empty(self, text: str) -> None:
        assert clean_transcript(text, "en") == ""

    def test_only_profanity_becomes_empty(self) -> None:
        assert clean_transcript("Shit!", "en") == ""

    def test_masks_profanity_in_speech(self) -> None:
        assert clean_transcript("Welcome, bitches.", "en") == f"Welcome, {MASK}."

    def test_ordinary_thanks_kept(self) -> None:
        assert clean_transcript("Thank you, Bishop.", "en") == "Thank you, Bishop."
