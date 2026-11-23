"""Small, dependency-free ISO 639-1 language vocabulary."""

from __future__ import annotations

import re

ISO_639_1_CODES = frozenset(
    """
    aa ab ae af ak am an ar as av ay az ba be bg bh bi bm bn bo br bs ca ce ch co cr cs cu cv cy da de dv dz ee el en eo es et eu fa ff fi fj fo fr fy ga gd gl gn gu gv ha he hi ho hr ht hu hy hz ia id ie ig ii ik io is it iu ja jv ka kg ki kj kk kl km kn ko kr ks ku kv kw ky la lb lg li ln lo lt lu lv mg mh mi mk ml mn mr ms mt my na nb nd ne ng nl nn no nr nv ny oc oj om or os pa pi pl ps pt qu rm rn ro ru rw sa sc sd se sg si sk sl sm sn so sq sr ss st su sv sw ta te tg th ti tk tl tn to tr ts tt tw ty ug uk ur uz ve vi vo wa wo xh yi yo za zh zu
    """.split()
)

_LANGUAGE_CODE_PATTERN = re.compile(r"^[a-z]{2}$", re.ASCII)


def normalize_language_code(value: object) -> str | None:
    """Return a current lowercase ISO 639-1 code, or ``None``."""
    if not isinstance(value, str):
        return None
    candidate = value.strip().casefold()
    if not _LANGUAGE_CODE_PATTERN.fullmatch(candidate) or candidate not in ISO_639_1_CODES:
        return None
    return candidate
