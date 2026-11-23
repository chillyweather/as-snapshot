# Issue #554 work-level claim page

Constructed fixture, not a captured page.

Both `issue-502` pages state their prominence language about the author, not the
work: "bestselling author Aaron Mahnke" and "award-winning podcast Lore".
Measured against the merged rule, neither page contains a single sentence that
names the work, asserts a claim, and does not name the author.
So they correctly support zero claims, which is what
`test_issue_554_both_exhumed_fixtures_support_no_claims` asserts.

A page carrying a genuine work-level claim is still needed to prove the rule
admits one, so this file supplies one sentence per allowed claim type.
It is written for that purpose and is not a copy of any real retailer page.
