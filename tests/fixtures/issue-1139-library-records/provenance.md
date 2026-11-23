# Library records for issue #1139

The `bnf-*.xml` files are the French National Library's SRU answers, saved verbatim on 2026-11-18 from
`https://catalogue.bnf.fr/api/SRU?version=1.2&operation=searchRetrieve&query=bib.isbn%20adj%20%22<ISBN>%22`:
a translation (Sapiens, 9782226257017: field 101 indicator 1 `1`, `$b eng` intermediate, `$c heb` original,
454 the title it was translated from, 215 `501 p.`), an original (L'anomalie, 9782072895098: 101 indicator 1 `0`),
and an ISBN the BnF does not hold (zero records).

The `dnb-*.xml` files are hand-built in the shape of the German National Library's SRU MARC21-xml answer
(`https://services.dnb.de/sru/dnb?version=1.1&operation=searchRetrieve&query=num%3D<ISBN>&recordSchema=MARC21-xml`).
On 2026-11-18 the DNB host refused TLS from the development machine, so no live answer could be saved;
field 041 (indicator 1 `1`, `$h` original language), 240 (original title) and 300 (extent) follow MARC 21.
Replace them with saved live answers once the host answers.
