"""Tests for the Russian-publication status vocabulary single source of truth.

Mirrors the pure-function test style in tests/test_shared_helpers.py: the
predicates are tested directly, independent of any pipeline plumbing.
"""

from __future__ import annotations

from autoscout.russian_status import FOUND_IN_RUSSIAN
from autoscout.russian_status import NOT_FOUND_IN_RUSSIAN
from autoscout.russian_status import RUSSIAN_STATUSES
from autoscout.russian_status import UNCLEAR
from autoscout.russian_status import is_found_in_russian
from autoscout.russian_status import is_not_found_in_russian
from autoscout.russian_status import is_unclear


class RussianStatusVocabularyTests:
    def test_canonical_constants_match_stored_artifact_casing(self):
        assert FOUND_IN_RUSSIAN == "found in Russian"
        assert NOT_FOUND_IN_RUSSIAN == "not found in Russian"
        assert UNCLEAR == "unclear"

    def test_valid_status_set_contains_exactly_the_three_constants(self):
        assert RUSSIAN_STATUSES == {FOUND_IN_RUSSIAN, NOT_FOUND_IN_RUSSIAN, UNCLEAR}


class IsFoundInRussianTests:
    def test_canonical_value(self):
        assert is_found_in_russian("found in Russian")

    def test_case_variant(self):
        assert is_found_in_russian("Found In Russian")
        assert is_found_in_russian("FOUND IN RUSSIAN")

    def test_whitespace_variant(self):
        assert is_found_in_russian("  found in Russian  ")

    def test_empty_and_none_like_values(self):
        assert not is_found_in_russian("")
        assert not is_found_in_russian(None)

    def test_non_status_string(self):
        assert not is_found_in_russian("unclear")
        assert not is_found_in_russian("not found in Russian")
        assert not is_found_in_russian("nonsense")


class IsNotFoundInRussianTests:
    def test_canonical_value(self):
        assert is_not_found_in_russian("not found in Russian")

    def test_case_variant(self):
        assert is_not_found_in_russian("Not Found In Russian")

    def test_whitespace_variant(self):
        assert is_not_found_in_russian("  not found in Russian  ")

    def test_empty_and_none_like_values(self):
        assert not is_not_found_in_russian("")
        assert not is_not_found_in_russian(None)

    def test_non_status_string(self):
        assert not is_not_found_in_russian("found in Russian")
        assert not is_not_found_in_russian("unclear")
        assert not is_not_found_in_russian("nonsense")


class IsUnclearTests:
    def test_canonical_value(self):
        assert is_unclear("unclear")

    def test_case_variant(self):
        assert is_unclear("Unclear")
        assert is_unclear("UNCLEAR")

    def test_whitespace_variant(self):
        assert is_unclear("  unclear  ")

    def test_empty_and_none_like_values(self):
        assert not is_unclear("")
        assert not is_unclear(None)

    def test_non_status_string(self):
        assert not is_unclear("found in Russian")
        assert not is_unclear("not found in Russian")
        assert not is_unclear("nonsense")
