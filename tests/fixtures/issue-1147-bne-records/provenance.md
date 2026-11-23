# Spanish National Library records for issue #1147

Every file is a Spanish National Library (BNE) SRU answer saved verbatim on 2026-11-19.
The ISBN answers come from
`https://catalogo.bne.es/view/sru/34BNE_INST?version=1.2&operation=searchRetrieve&recordSchema=marcxml&query=alma.isbn=<ISBN>`:

- `bne-9788419558916-translation.xml`: Las leyes de la competición. Two records: the e-book (240 `$l Español`, no 041) and the print edition (`041 1 $a spa $h eng`, 300 `519 páginas`).
- `bne-9788499926223-translation.xml`: Sapiens. The e-book (240 `$l`, no 041) and the print edition (`041 1 $a spa $k eng $h heb`, 240 the romanised Hebrew title, 300 `492 p.`).
- `bne-9788410313064-ebook.xml`: the e-book ISBN of Las leyes de la competición; only the e-book record, so 240 `$l` is its only translation statement.
- `bne-9788466347518-thin.xml`: a provisional record with neither 041 nor 240.
- `bne-9788497592208-original.xml`: Cien años de soledad, three records with a 240 uniform title and no `$l`.
- `bne-9788401039690-no-record.xml`: a recent ISBN the BNE does not hold (zero records).

Two title searches (`maximumRecords=4`) cover cases no list ISBN gave:

- `bne-iliada-greek-original-text.xml`: `alma.title="Ilíada" and alma.creator="Homero"`; Greek text editions with `041 0` and a 240 without `$l`.
- `bne-yo-confieso-catalan.xml`: `alma.title="Yo confieso" and alma.creator="Cabré"`; a Catalan-to-Spanish translation (`$h cat`, 240 `Jo confesso $l Español`), one record with 041 indicator 1 blank.

Verification beyond the fixtures: 45 ISBNs were asked (13 chosen books, 32 from Spanish lists in Candidate History).
Of the 32 recent list ISBNs the BNE held 6, none of them with a translation statement beyond one Italian translation (240 `$l` and `041 1 $h ita`).
Every Spanish original seen (Cien años de soledad, Patria, El infinito en un junco, La herida, Toda la poesía, a Lorca play) has a 240 without `$l`; every translation has 240 `$l Español`, 041 `$h`, or both.
