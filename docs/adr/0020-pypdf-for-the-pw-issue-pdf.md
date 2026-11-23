# Optional pypdf for PW issue PDFs

The PW Digital Library pass uses pypdf, pinned exactly in the `pw` extra.
This is a scoped exception to the zero-runtime-dependency design in ADRs
0007, 0009 and 0010: the core installation remains stdlib-only, and only
the PW PDF adapter imports pypdf, lazily. An unavailable reader produces
`pw_pdf_reader_missing` for this pass alone.

The 2026-11-04 investigation recorded in #954 found the same 32 nonfiction
ISBNs with pypdf as with poppler's `pdftotext` on the 2 November issue, in
about one second. Poppler would require another system tool on the editor's
Mac; a handwritten stdlib PDF reader would be fragile. pypdf keeps this
source portable and leaves a pure page-text parser seam.

Written PW permission is recorded on #935 (2026-11-03). PDFs exist only in
a temporary directory during processing and are deleted even after failure.
Full review text, subscriber links, tokens and cookies are never retained.
Only bibliographic rows and counts leave the pass. Test inputs are invented.
