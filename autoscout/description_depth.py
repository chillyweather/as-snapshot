"""Mechanical depth assessment for Russian Content Descriptions (#672).

Safety validation (``content_description.validate_content_description``)
answers "is this safe Russian prose?". Depth assessment answers a
different, narrower question: "does this text clear a mechanical floor of
at least 200 normalized characters in at least two sentences?". The two
verdicts are deliberately separate artifacts:

* Safety stays exactly as it was; ``validated_count`` semantics do not
  change here.
* Depth never claims literary quality, factual completeness, or
  grounding. A long invented paragraph clears the floor; a short perfect
  summary does not. Editor-facing wording must stay mechanical (a "short
  description" marker, never a quality verdict).

Policy (version 1):

* ``DEPTH_MIN_CHARS = 200`` counted over whitespace-normalized text
  (``" ".join(value.split())`` — the same normalization the safety
  validator returns).
* ``DEPTH_MIN_SENTENCES = 2`` sentences over the normalized text.

Sentence boundaries are deterministic stdlib-only heuristics, documented
here because every consumer must split identically:

* A boundary is a run of period, exclamation, question, or ellipsis
  marks followed by whitespace or the end of text. Two-mark clusters and
  three-period runs are one boundary each.
* No boundary inside a decimal or numbered reference: a period with a
  digit on both sides never splits.
* No boundary after a known abbreviation. The closed list (exact Cyrillic
  forms in ``_DEPTH_ABBREVIATIONS`` below) covers Russian editorial
  abbreviations — the "t.d."/"t.e."/"t.n."/"t.k."/"t.o." family, "i t.d.",
  "i t.p.", "i dr.", "i pr.", "g.", "gg.", "ul.", "d.", "str.", "ris.",
  "tabl.", "sm.", "napr.", "v t.ch.", "t.", "ch.", "vyp.", "izd.", "per.",
  "red.", "sost.", "akad.", "prof.", "dots.", "kand.", "im.", "sv." — and
  Latin ones common in mixed text ("e.g.", "i.e.", "etc.", "Mr.", "Mrs.",
  "Dr.", "vs.", "approx.", "No.", "p.", "pp.", "ed.", "vol.", "St.").
  Matching is case-insensitive; multi-word entries must match whole.
* No boundary after a single-letter initial (a lone Cyrillic or Latin
  letter plus period). A final period of a known dotted abbreviation run
  stays live, since the abbreviation may end its sentence; any other
  run-final single letter stays protected as an initial.
* An ellipsis terminates a sentence only when followed by whitespace or
  the end of text; an ellipsis glued between words is not a boundary.
* A text with no terminator at all is one sentence when non-blank.
* A segment counts as a sentence only when it holds at least one letter
  or digit, so trailing punctuation debris never inflates the count.
"""

from __future__ import annotations

import re
from typing import Any

# Versioned policy: every assessment records this version, so a future
# floor change can distinguish artifacts assessed under each policy
# instead of silently reinterpreting old counts (issue #672).
DEPTH_POLICY_VERSION = 1
DEPTH_MIN_CHARS = 200
DEPTH_MIN_SENTENCES = 2

# Closed abbreviation list, stored lowercase without the trailing period.
# Multi-word entries match across single spaces after normalization.
_DEPTH_ABBREVIATIONS = frozenset(
    {
        "т.д",
        "т.е",
        "т.н",
        "т.к",
        "т.о",
        "и т.д",
        "и т.п",
        "и др",
        "и пр",
        "г",
        "гг",
        "ул",
        "д",
        "стр",
        "с",
        "рис",
        "табл",
        "см",
        "напр",
        "в т.ч",
        "т",
        "ч",
        "вып",
        "изд",
        "пер",
        "ред",
        "сост",
        "акад",
        "проф",
        "доц",
        "канд",
        "им",
        "св",
        "e.g",
        "i.e",
        "etc",
        "mr",
        "mrs",
        "ms",
        "dr",
        "vs",
        "approx",
        "no",
        "p",
        "pp",
        "ed",
        "vol",
        "st",
    }
)

# Dotted-run cores (periods removed, lowercased) whose final period stays
# a live sentence boundary: the abbreviation may end its sentence
# ("Он уехал т.д. И всё."). Anything else closing a masked run is an
# initial ("J.R.R. Tolkien") and stays protected.
_DOTTED_RUN_CORES = frozenset({"тд", "те", "тн", "тк", "то", "eg", "ie"})

_DECIMAL_PERIOD = re.compile(r"\d\.\d")
_MASK_TOKEN = "\ue000"


def normalize_depth_text(value: Any) -> str:
    """Collapse whitespace the way the safety validator normalizes."""
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())


def _mask_protected_periods(text: str) -> str:
    """Replace periods that must not split sentences with a mask token."""
    masked = _DECIMAL_PERIOD.sub(lambda match: match.group(0).replace(".", _MASK_TOKEN), text)

    def _mask_dotted_run(match: re.Match[str]) -> str:
        # A dotted run ("т.д.", "e.g.") keeps its final period live: the
        # abbreviation may well end the sentence ("Он уехал т.д. И всё.").
        # Only internal periods are protected.
        run = match.group(0)
        head, _separator, _tail = run.rpartition(".")
        return head.replace(".", _MASK_TOKEN) + "."

    # Tight dotted runs first, so "т.д." cannot match "т." as one unknown
    # token and leave the real abbreviation half-masked.
    masked = re.sub(r"[A-Za-zА-Яа-яЁё](?:\.[A-Za-zА-Яа-яЁё])+\.", _mask_dotted_run, masked)
    # Spaced multi-word abbreviations ("в т.ч.", "и т.д." with spaces).
    masked = re.sub(
        r"и\s+т\.[ ]?д\.|и\s+т\.[ ]?п\.|и\s+др\.|и\s+пр\.|в\s+т\.[ ]?ч\.",
        _mask_dotted_run,
        masked,
        flags=re.IGNORECASE,
    )

    def _mask_token(match: re.Match[str]) -> str:
        token = match.group(0)
        core = token[:-1].casefold()
        # A single letter plus period is an initial, always protected —
        # even inside a dotted run ("J.R.R. Tolkien") — unless it closes a
        # known dotted abbreviation ("т.д."), whose final period stays live.
        if len(core) == 1 and core.isalpha():
            if match.start() > 0 and masked[match.start() - 1] == _MASK_TOKEN:
                position = match.start() - 1
                run_letters = [core]
                while position >= 1 and masked[position] == _MASK_TOKEN and masked[position - 1].isalpha():
                    run_letters.append(masked[position - 1].casefold())
                    position -= 2
                if "".join(reversed(run_letters)) in _DOTTED_RUN_CORES:
                    return token
            return token[:-1] + _MASK_TOKEN
        # A token closing a dotted run keeps its live period (see above).
        if match.start() > 0 and masked[match.start() - 1] == _MASK_TOKEN:
            return token
        if core in _DEPTH_ABBREVIATIONS:
            return token[:-1] + _MASK_TOKEN
        return token

    # Single-word abbreviations and initials: one letter-run plus period.
    return re.sub(r"[A-Za-zА-Яа-яЁё]+\.", _mask_token, masked)


def split_depth_sentences(normalized: str) -> list[str]:
    """Split normalized text into sentences per the module policy."""
    if not normalized.strip():
        return []
    masked = _mask_protected_periods(normalized)
    # One boundary per terminator cluster: runs of . ! ? ellipsis chars,
    # split only before whitespace or the end of text.
    parts = re.split(r"[.!?…\u2026]+(?=\s|$)", masked)
    sentences: list[str] = []
    for part in parts:
        restored = part.replace(_MASK_TOKEN, ".").strip()
        if restored and re.search(r"[A-Za-zА-Яа-яЁё0-9]", restored):
            sentences.append(restored)
    return sentences


def assess_description_depth(value: Any) -> dict[str, Any]:
    """Assess one description against the mechanical depth floor.

    Returns ``{"meets_floor", "char_count", "sentence_count",
    "policy_version"}``. Non-string and blank inputs assess as below the
    floor with zero counts — depth is a property of text, and missing text
    has none. The assessment never validates safety; callers partition
    safety-validated descriptions with it.
    """
    normalized = normalize_depth_text(value)
    char_count = len(normalized)
    sentences = split_depth_sentences(normalized)
    sentence_count = len(sentences)
    meets_floor = char_count >= DEPTH_MIN_CHARS and sentence_count >= DEPTH_MIN_SENTENCES
    return {
        "meets_floor": meets_floor,
        "char_count": char_count,
        "sentence_count": sentence_count,
        "policy_version": DEPTH_POLICY_VERSION,
    }


def meets_description_depth(value: Any) -> bool:
    """True when *value* clears the mechanical depth floor."""
    return bool(assess_description_depth(value)["meets_floor"])
