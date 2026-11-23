# Issue #457 closure acceptance fixtures

These are reduced live captures collected on 2026-08-17 with bounded unauthenticated GET requests.

The files are reduced live captures, not synthetic source pages.

They retain only the real identity and subject-bearing elements needed by `BoundedHTTPFetcher`.
They omit page chrome, scripts, full reviews, and large article bodies.

Exact source URLs:

- https://bookmarks.reviews/reviews/a-guide-to-open-water-lifesaving-lessons-on-love-care-and-survival-a-memoir/
- https://www.kirkusreviews.com/book-reviews/virginia-eubanks/a-guide-to-open-water-lifesaving/
- https://bookmarks.reviews/reviews/all-thats-unseen-an-appalachian-memoir/
- https://www.penguinrandomhouse.com/books/748921/all-thats-unseen-by-emilee-hackney/

The Book Marks files are detail review pages, not category index pages.
Their retained review pull quotes are the bounded subject evidence used by the acceptance tests.
The tests cover the MVP Pipeline path through source classification, bounded fetch, grounding, bounded synthesis input, and a grounded Content Description in StageOutcome.
They do not modify Candidate History or a Weekly Report Row.
