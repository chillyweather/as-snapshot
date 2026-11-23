# A bounded annual award list is its own contract family

Issue: #577, amended by #578 and #795.

## Context

An annual award shortlist page could pass neither contract family.

`exact-book-list` grounds a mention in a book-level title link.
A shortlist page usually has none: `Memorial Days, Geraldine Brooks (Hachette Australia)` is one line of plain text.

`ranking-table` accepts unlinked rows, but describes a recurring ranking.
It requires a reporting period or the undated row-movement declaration of ADR 0012, a rank, an identifier, and title exclusion terms.
An award has no weekly period and no freshness window, its rows are unranked and carry a publisher rather than an ISBN, and its own rules already exclude fiction.
A declared 365-day maximum age, a document-order rank, or filler exclusion terms would each be a declaration that states nothing true about the page.
The undated policy of ADR 0012 is no substitute either: it reports an unchanged list as frozen, and an award shortlist is unchanged by design until the next edition.

Eight previously rejected first-party sources and two open issues, #573 and #574, sat behind this.

## Decision

A bounded annual award list declares `contract_family: "award-list"`.
The family lives in `autoscout/award_list.py` and adds no collector code: the page is fetched and extracted like any other configured page, and the family supplies the declaration, the page check, and the grounding.

`ranking-table` is unchanged.
Not one of its declarations became optional, and `award-list` shares no validator branch with it.
An `award-list` source that declares any `ranking_*` field or `adult_nonfiction_exclusions` is refused, because nothing would read them.
An `expected_sort_label` is refused too: an award category is a set with no sort order, and the replay page keeps only the headings, the year, and the rows.

The source declares five things, and may declare six more.

`award_year` is the edition year.
`award_category_heading` is the exact visible heading above the rows.
`award_row_selector` names the row element: `li`, `p`, `tr`, `dd`, `div`, or a class selector.
`award_row_format` is one row written out with placeholders, for example `{title}, {author} ({ignored})`.
`award_expected_row_count` is the exact number of rows.
`award_year_selector` is optional and names the one element that states the year; without it the year is read from the visible headings outside the award rows.
It names a label, not a container: an element that contains an award row, sits inside one, or holds more than 200 characters is refused, because a row title such as `Life in 2026` would otherwise supply the year.
`award_author_conjunction` is optional and is the word this page puts between the last two author names, such as `and` or `och`.
`award_row_split_first` is optional and states that when a row repeats a separator, the first one divides title and author.
`award_additional_category_headings` is optional and lists the exact headings of further categories of the same award on the same page (#578).
`award_row_title_element` is optional and names the emphasis element each row sets its title in: `em`, `i`, `cite`, `b`, or `strong` (#578).
`award_category_label_element` is optional and names the bold element, `b` or `strong`, a page sets a whole category label paragraph in when it uses no heading element (#795).

A row format is a template rather than a regular expression.
`{title}` and `{author}` appear exactly once, `{rank}` and `{identifier}` are optional, and `{ignored}` stands for text the row carries and the mention does not.
Two placeholders must be separated by literal text.
A template cannot be malformed into a pattern that matches everything, and it reads like the row it describes.

Rank and identifier are optional because the format decides them.
A format without `{rank}` describes an unranked list, and a mention that supplies a rank for it is refused as ungrounded.
A format with `{rank}` lets a mention carry that rank, checked against its row, without requiring it.
The scaffold and the configuration loader both call `validate_declaration`, so they cannot disagree about what is required.

The source cadence must be annual.
The annual source audit is what reports a list one edition behind, and it reads only sources that declare an annual cadence.

## What fails closed

The category is the text between its heading and the next visible heading of the same or a higher level.
The heading must appear exactly once.
A renamed, removed, or repeated heading refuses the page.

A news article often labels its lists with a bold paragraph, `<p><strong>Nominowani w 2026 roku</strong></p>`, and not with a heading element (#795).
A source that declares `award_category_label_element` reads its category headings from such paragraphs instead.
A label is a visible paragraph whose whole visible text is set in exactly one element of the declared kind.
A paragraph with text outside that element, with two such elements, or with a row inside it is prose and bounds nothing.
The category runs from its label to the next label paragraph, whatever that one says, or to the next heading element of any level.
On such a page a heading element with the declared text is not the category, so a page that changes its labels to headings is refused until the declaration is changed.
The declaration cannot be combined with `award_row_selector: p`, because a paragraph cannot be both a row and the label that bounds the rows.

Every row in the category must match the format with a nonblank title and a nonblank author.
One row that does not refuses the page, so a row that loses its title or author is never dropped quietly.
A row that can be divided into title and author in more than one way refuses the page as well.
Every way of dividing the row among the format's fields is tried, so a second reading is found whether the repeated separator sits in the title, the author list, or ignored text.
A row longer than 500 characters is prose rather than a title and author line, and is refused.
`The Long Coast, A Memoir, Mara Ellison (Press)` reads equally as a title with a subtitle or as a title with two authors, and a mention is grounded against whichever reading the parser took, so a wrong reading would ground itself.
A source that declares `award_row_split_first: true` takes responsibility for the first reading; without the declaration the page is refused.
An empty row element is a row that states neither, and refuses the page too.

A source that declares `award_row_title_element` reads the title from the page's own markup instead of from punctuation.
`<em>Always Home, Always Homesick</em>, Hannah Kent (Picador)` has a comma inside its title, and the first separator is the wrong one.
The marked element must begin where the title begins, and end where the title ends or inside the separator that follows it: a page writes `<em>Guts</em>, ` and `<em>Guts, </em>` for the same row.
A row with no such element, with two, with an empty or hidden one, or with one that covers only part of the title or runs into the author refuses the page.
The declaration and `award_row_split_first` are two rules for the same boundary, and a source that declares both is refused.

The row count must equal the declared count.
A shortlist that grows, shrinks, or is replaced by its winner refuses the page until the declaration is changed.
With `award_additional_category_headings`, every declared heading must appear exactly once and every declared category must hold the declared count.
A book listed under two declared categories refuses the page: it is one book and one mention, and two rows would expect two.

Each mention must match one row of the category by exact title.
Its primary authors must be exactly the complete names the row lists.
The author text is split on `,`, `;`, `/`, `&`, and the declared `award_author_conjunction`.
A truncated name, a name split in two, several names joined into one, a wrong name, and a partial list are all refused.
The conjunction is declared rather than derived from the source language, so `y` separates two authors only on a page that declares it and stays inside `José Ortega y Gasset` everywhere else.
The same split gives the author display the probe reports and the helper sidecar is keyed by: the names joined with ` / `, which is how collection joins a mention's authors when it looks the sidecar up.
A name that itself contains a separator, such as `Martin Luther King, Jr.`, cannot be stated by the split: a row whose author list contains a bare name suffix (`Jr.`, `Sr.`, `III`, `PhD`, an honour such as `AM`) refuses the page, because reading it would publish a cut name and a coauthor who does not exist.
A row is matched to the format by a bounded search, not by running the pattern, so a row that almost fits is refused as quickly as one that does not fit at all.
A supplied `isbn` or `current_rank` must be the value its row states; any other nonblank value, including a placeholder such as `N/A`, is refused.
`source_url` and `evidence_url` must both be the configured page.
A supplied `source_week` is refused, because the page states no period.
A mention may carry only the fields a row can ground, plus the helper metadata; a publisher, a publication year, or a description is refused as an unsupported fact.

## The award year

The page must state the declared year, and must not state a newer one.
A page that shows 2027 while the source declares 2026 has rolled over, and its rows are no longer the declared rows.

The declared year is then aged against the reference date of the run or the capture.
A year more than one behind is refused as stale.
A year more than one ahead is refused as unpublished.
A capture with no reference date is refused, because a year that cannot be aged is not known to be current.

A year exactly one behind is collected and reported.
Most nonfiction awards announce late in the year, so a 2026 list in mid-2027 is usually the latest edition, and refusing it would silence a correct source for months.
The annual source audit reads `award_year` and reports such a source as `rollover-check-required` in the run record.

## Consequences

Existing ranking-table sources are unaffected.
PublishNews, Publishers Weekly, and the Audible and Amazon charts keep their declarations and their verdicts.

A page that lists several award categories is one source (#578).
A Configured Source is identified by its URL: the Source Proposal, the Source Candidate Ledger entry, the lifecycle metrics, and the source-run record are all keyed by it.
Two sources on one URL would share one proposal and merge their metrics, so the first decision here, one source per category, could not be carried out.
The source declares its first category in `award_category_heading` and the others in `award_additional_category_headings`.
The categories share the year, the row element, the row format, and the row count.
A category that is not adult nonfiction is left undeclared; it is not filtered by title terms.

The replay fixture retains each declared category heading, the text that states the year, and the row lines with their declared title element.
A declared category label stays a bold paragraph in the fixture.
It does not retain other categories on the same page.

Four shapes are not covered.
A row whose title and author sit in separate elements with no literal text between them cannot be described by a format.
A category whose rows are themselves headings ends at its first row.
A row whose title and author boundary is ambiguous, for which the first separator is not the right one, and whose title is not set in one emphasis element cannot be read.
Declared categories of different sizes cannot share one row count.
Each needs its own declaration and is left until a source requires it.
