import pytest

from autoscout.source_page_identity import _validate_date_format


@pytest.mark.parametrize("date_format", ["%d.%m.%Y", "%Y.%m.%d", "%d/%m/%Y", "%Y-%m-%d"])
def test_ranking_date_format_accepts_dotted_and_existing_formats(date_format: str) -> None:
    assert _validate_date_format(date_format) == date_format


def test_ranking_date_format_still_rejects_invalid_formats() -> None:
    with pytest.raises(ValueError, match="not a valid date format"):
        _validate_date_format("%Q")
