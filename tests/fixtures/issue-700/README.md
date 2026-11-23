# Issue #700 part A fixtures

Frozen copies of real captures from run `4090c59a` (2026-09-13), used by
`tests/test_issue_700_reason_separation.py` to prove the reason separation
works on real bytes rather than only on synthetic dicts.

Copied verbatim; do not edit. Verify with:

```
sha256sum -c SHA256SUMS
```

- `audiobooks-shell-capture.json` — 198,094-byte JS shell (`Bestseller` ×0,
  `Nonfiction` ×0). Extraction returned nothing; reproduces zero.
- `todostuslibros-homepage-capture.json` — 629,894 bytes resolving to the
  site homepage, not the declared recommendation list.
- `carnegie-content-capture.json` — 362,878 content-rich bytes (`Carnegie`
  ×241, `Longlist` ×142) that still extracted zero.
