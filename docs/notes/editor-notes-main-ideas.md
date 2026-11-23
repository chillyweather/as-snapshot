# Editor Notes - Main Ideas

Summarised from two recorded conversations with the editor.
The source audio is not part of this repository.

## Main Direction

- The system is moving in the right direction. The value is not perfect selection of 200 books, but reducing the manual work of scanning many sources and surfacing a finite list worth reviewing.
- A weekly review cadence is enough. More frequent updates are not necessary from the user's side, though the implementation can run however is convenient.
- The current list/table format is comfortable because it is visible, editable, and leaves room for comments.

## Book Age And Editions

- Do not exclude older books by default. Some older books are still foundational and useful, especially when newer books are distorted by current political/cultural topics or cannot be published locally.
- Old books may sometimes be more publishable in Russia because they avoid current sensitive topics.
- Editions of the same book in different languages do not need special handling right now. First observe whether duplicates across markets actually appear often.
- If the same book appears in several countries, that may itself be a useful signal, not necessarily a problem.

## Duplicate And Feedback Handling

- The system should avoid adding the same book again as a fresh candidate.
- It could instead update an existing candidate's ranking or priority when new evidence appears.
- There should be a way to mark books as "do not show again" or otherwise give negative feedback.
- More generally, the system should learn from curator/editor feedback: why a recommendation was rejected, why something was useful, and what criteria matter.
- This feedback loop is not urgent, but it would make the tool increasingly useful over time.

## Candidate Analysis

- After initial discovery, the next useful layer is deeper analysis of why a book is popular or interesting.
- Useful questions:
  - Why did this book unexpectedly become popular?
  - Are readers saying it changed how they think or gave them a practical framework?
  - Is popularity driven by the author's social media audience rather than the book's content?
  - Does the book fit the category only nominally, while being commercially irrelevant for the target market?
- Reviews are important evidence, especially repeated reader patterns and concrete reasons for enthusiasm.
- Open public materials could also help: table of contents, previews, sample pages, and other metadata.

## Market Fit

- A book can be a hit in the US/UK and still be a poor fit for Russia.
- Example pattern: a celebrity-adjacent business/personality book may perform well abroad because of local fame, but be hard to sell in Russia if the person or cultural context is not meaningful there.
- The tool should help identify both "why this works there" and "why it probably does not work here."

## Reporting / Updates

- After the initial large discovery pass, later runs should probably behave more like updates.
- Potential update format:
  - new candidates;
  - candidates that improved the most;
  - candidates with the strongest new evidence;
  - candidates that changed position significantly.
- This may be optional or later-stage; the immediate value is already in collecting sources and reducing the list to something reviewable.

## Not Urgent But Useful Later

- Build a more explicit feedback mechanism for curator decisions.
- Track previously seen candidates and avoid re-surfacing them as new.
- Distinguish "already reviewed and rejected" from "still active but lower priority."
- Add richer reasoning for recommendations based on reviews, previews, and public signals.
- Consider a weekly digest/update mode after the first full run.
