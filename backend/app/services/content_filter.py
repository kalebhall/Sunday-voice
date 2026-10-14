"""Profanity masking and transcription-hallucination cleanup.

Neither the OpenAI transcription API nor Google Translation v2 offers a
profanity filter, and both models will happily produce offensive words when
the audio is noisy, clipped, or misheard.  Every transcript and translation
passes through this module before it is persisted, shown, or spoken:

* :func:`clean_transcript` removes phrases the transcription model invents on
  silence or noise (``"Thanks for watching"``, Amara.org credits, ...) and
  masks profanity.  Returns ``""`` when nothing worth publishing is left.
* :func:`mask_profanity` replaces offensive words with :data:`MASK`.
* :func:`strip_profanity` drops offensive words entirely; used on the text we
  send to the translator so it never sees (or echoes) them.
* :func:`for_speech` removes masks so TTS never reads "asterisk asterisk".

Word lists deliberately leave out words that are offensive in casual speech
but appear in scripture or sermons (hell, damn, ass, whore, cock, shittim, ...)
so legitimate meeting content is never masked.  Operators can add words with
the ``CONTENT_FILTER_EXTRA_WORDS`` setting.
"""

from __future__ import annotations

import re
from functools import lru_cache

from app.core.config import get_settings

MASK = "***"

# Regex fragments matched as whole words (case-insensitive).  English applies
# to every language because the transcription model sometimes emits English
# words even when told the source language is something else.
_PROFANITY_PATTERNS: dict[str, tuple[str, ...]] = {
    "en": (
        r"\w*fuck\w*",
        r"(?:bull|horse|dip|chicken)?shit(?!tim)\w*",
        r"cunt\w*",
        r"bitch\w*",
        r"\w*asshole\w*",
        r"nigg(?:er|a|ah|az)s?",
        r"fag(?:s|got\w*)?",
        r"puss(?:y|ies)",
        r"twats?",
        r"wank\w*",
        r"sluts?",
        r"dickheads?",
        r"cocksuck\w*",
        r"tits",
        r"dildos?",
        r"blowjobs?",
        r"jackass\w*",
        r"goddamn\w*",
    ),
    "es": (
        r"put[ao]s?",
        r"pendej\w*",
        r"cabr[oó]n\w*",
        r"cabronas?",
        r"ching\w*",
        r"vergas?",
        r"culer[oa]s?",
        r"culos?",
        r"mierdas?",
        r"coños?",
        r"joder",
        r"jodid[oa]s?",
        r"maric[oó]n\w*",
        r"pinche\w*",
        r"carajo",
        r"gilipollas",
    ),
    "tl": (
        r"putang\s*ina\w*",
        r"tang\s*ina\w*",
        r"put[ao]",
        r"gag[oa]",
        r"tarantad[oa]\w*",
        r"ulol",
        r"kupal",
        r"pakyu",
        r"pakshet",
        r"tite",
        r"puke",
        r"kantot\w*",
        r"jakol\w*",
        r"burat",
    ),
}

# Phrases the transcription model hallucinates on silence, music, or noise
# (learned from subtitled video in its training data).  Removed wherever they
# appear in a transcript.
_HALLUCINATION_PATTERNS: tuple[str, ...] = (
    # First, so the dot in "Amara.org" doesn't end a shorter match early.
    r"[^.!?]*amara\.org[^.!?]*[.!?]?",
    r"thanks? (?:you )?(?:so much )?for watching[^.!?]*[.!?]?",
    r"(?:please )?(?:like(?:,)? (?:and|&) )?subscribe(?: to (?:my|the|our) channel)?[.!?]?",
    r"see you (?:in the )?next (?:video|time)[.!?]?",
    r"subtitles? (?:by|created by|provided by)[^.!?]*[.!?]?",
    r"subt[ií]tulos? (?:realizados? )?por[^.!?]*[.!?]?",
    r"gracias por ver(?: el v[ií]deo)?[.!?]?",
    r"suscr[ií]bete[^.!?]*[.!?]?",
)

_HALLUCINATION_RE = re.compile("|".join(_HALLUCINATION_PATTERNS), re.IGNORECASE)
_MASK_RE = re.compile(re.escape(MASK))
# Collapses whitespace and stray punctuation left behind after removals.
_SPACE_RE = re.compile(r"\s{2,}")
_SPACE_BEFORE_PUNCT_RE = re.compile(r"\s+([,.!?;:])")


def _base_language(language: str) -> str:
    return language.split("-")[0].lower()


@lru_cache(maxsize=32)
def _profanity_re(language: str, extra_words: str) -> re.Pattern[str]:
    patterns = list(_PROFANITY_PATTERNS["en"])
    base = _base_language(language)
    if base != "en":
        patterns.extend(_PROFANITY_PATTERNS.get(base, ()))
    patterns.extend(
        re.escape(word.strip()) for word in extra_words.split(",") if word.strip()
    )
    return re.compile(r"\b(?:" + "|".join(patterns) + r")\b", re.IGNORECASE)


def _enabled_pattern(language: str) -> re.Pattern[str] | None:
    settings = get_settings()
    if not settings.content_filter_enabled:
        return None
    return _profanity_re(language, settings.content_filter_extra_words)


def _tidy(text: str) -> str:
    text = _SPACE_BEFORE_PUNCT_RE.sub(r"\1", text)
    return _SPACE_RE.sub(" ", text).strip()


def mask_profanity(text: str, language: str) -> str:
    """Replace offensive words in *text* with :data:`MASK`."""
    pattern = _enabled_pattern(language)
    if pattern is None:
        return text
    return pattern.sub(MASK, text)


def strip_profanity(text: str, language: str) -> str:
    """Remove offensive words (and any existing masks) from *text*."""
    pattern = _enabled_pattern(language)
    if pattern is not None:
        text = pattern.sub("", text)
    return _tidy(_MASK_RE.sub("", text))


def for_speech(text: str) -> str:
    """Return *text* with masks removed so TTS doesn't read them aloud."""
    return _tidy(_MASK_RE.sub("", text))


def _has_words(text: str) -> bool:
    """True if *text* still has something other than masks and punctuation."""
    return bool(re.search(r"\w", _MASK_RE.sub("", text)))


def clean_transcript(text: str, language: str) -> str:
    """Remove hallucinated phrases and mask profanity in a source transcript.

    Returns ``""`` when nothing but masks or punctuation is left, so callers
    can skip publishing the segment entirely.
    """
    if get_settings().content_filter_enabled:
        text = _tidy(_HALLUCINATION_RE.sub("", text))
    text = mask_profanity(text, language)
    return text if _has_words(text) else ""
