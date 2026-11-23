"""Where the ranking HTML sits inside a fetched response body.

Every bounded fetch treated the whole response body as the page.  That is
right for a page a browser renders directly, and wrong for a ranking served
by a form endpoint, which commonly answers with a JSON envelope whose HTML
fragment is one escaped string field.  Bokhandlerforeningen Boklista is the
recorded case: its ``admin-ajax.php`` route answers
``{"html": "...", "post_count": 26, "last": false}``, and the escaped
fragment inside ``html`` cannot be parsed as markup until it is unwrapped.

A source may now declare the field that holds its HTML.  The declaration is
narrow by construction:

* It is a declaration on the source, never a search.  One top-level field
  name is named by the operator; no nested path is walked, and no field is
  guessed by looking for something that resembles markup.
* Unwrapping is the only step.  The extracted fragment is judged by the
  unchanged bounded evidence contract, so a JSON-wrapped ranking proves the
  same rows, links, and reporting period as any other page.
* It fails closed.  A body that is not JSON, an envelope without the declared
  field, and a field that is not a string are each a distinct refusal with
  its own reason, never a silent fallback to the raw body.
* A source that declares nothing keeps the unchanged whole-body contract.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from typing import Any

RANKING_RESPONSE_HTML_FIELD = "ranking_response_html_field"
RANKING_RESPONSE_LAST_PAGE_FIELD = "ranking_response_last_page_field"

REASON_RESPONSE_NOT_JSON = "response_not_json"
REASON_RESPONSE_FIELD_MISSING = "response_html_field_missing"
REASON_RESPONSE_FIELD_NOT_TEXT = "response_html_field_not_text"


@dataclass(frozen=True)
class UnwrappedResponse:
    """The HTML a bounded response yielded, or the reason it yielded none."""

    html: str = ""
    reason_code: str = ""
    reason: str = ""
    envelope: Mapping[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """Return whether the response yielded usable HTML."""
        return not self.reason_code

    def last_page_value(self, source: Mapping[str, Any]) -> bool | None:
        """Return the declared final-page value, or ``None`` when unusable."""
        name = str(source.get(RANKING_RESPONSE_LAST_PAGE_FIELD, "") or "").strip()
        if not name:
            return None
        value = self.envelope.get(name)
        return value if isinstance(value, bool) else None

    def is_last_page(self, source: Mapping[str, Any]) -> bool:
        """Return whether the envelope declares this the final page.

        This compatibility helper keeps the old fail-safe behaviour for
        callers that do not walk a declared page sequence. Strict paged
        collection uses :meth:`last_page_value` and refuses an unusable flag.
        """
        value = self.last_page_value(source)
        return True if value is None else value


def ranking_response_field(source: Mapping[str, Any]) -> str:
    """Return the declared envelope field name, empty when none is declared."""
    return str(source.get(RANKING_RESPONSE_HTML_FIELD, "") or "").strip()


def _validate_field_name(source: Mapping[str, Any], key: str) -> str:
    value = source.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        return f"{key} must be a string"
    if not value.strip():
        return f"{key} must be a nonempty field name"
    if value != value.strip():
        return f"{key} must not have leading or trailing whitespace"
    return ""


def validate_ranking_response_declaration(source: Mapping[str, Any]) -> str:
    """Return the declaration error for the envelope fields, if any."""
    for key in (RANKING_RESPONSE_HTML_FIELD, RANKING_RESPONSE_LAST_PAGE_FIELD):
        error = _validate_field_name(source, key)
        if error:
            return error
    if source.get(RANKING_RESPONSE_LAST_PAGE_FIELD) is not None and not ranking_response_field(source):
        return f"{RANKING_RESPONSE_LAST_PAGE_FIELD} requires {RANKING_RESPONSE_HTML_FIELD}"
    return ""


def unwrap_ranking_response(body: str, source: Mapping[str, Any]) -> UnwrappedResponse:
    """Return the ranking HTML this body carries under the source declaration.

    A source that declares no envelope field gets its body back unchanged, so
    every existing source keeps the whole-body contract exactly.
    """
    name = ranking_response_field(source)
    if not name:
        return UnwrappedResponse(html=body)
    try:
        envelope = json.loads(body)
    except (ValueError, RecursionError):
        return UnwrappedResponse(
            reason_code=REASON_RESPONSE_NOT_JSON,
            reason=f"response is not JSON, so {name!r} cannot be read",
        )
    if not isinstance(envelope, Mapping):
        return UnwrappedResponse(
            reason_code=REASON_RESPONSE_NOT_JSON,
            reason=f"response is not a JSON object, so {name!r} cannot be read",
        )
    if name not in envelope:
        return UnwrappedResponse(
            reason_code=REASON_RESPONSE_FIELD_MISSING,
            reason=f"response JSON has no {name!r} field",
        )
    value = envelope[name]
    if not isinstance(value, str):
        return UnwrappedResponse(
            reason_code=REASON_RESPONSE_FIELD_NOT_TEXT,
            reason=f"response field {name!r} is not text",
        )
    return UnwrappedResponse(html=value, envelope=envelope)
