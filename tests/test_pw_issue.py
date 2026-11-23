"""Invented PW layouts and a fake subscriber session; no licensed text or PDF fixtures."""

import base64
import builtins
import io
import json
import urllib.request
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from autoscout.pm_mailbox import MessageHeader
from autoscout.pw_issue_fetch import PwError
from autoscout.pw_issue_fetch import _Redirects
from autoscout.pw_issue_fetch import checked_url
from autoscout.pw_issue_fetch import fetch_issue
from autoscout.pw_issue_fetch import issue_link
from autoscout.pw_issue_fetch import pdf_page_texts
from autoscout.pw_issue_parser import parse_pages
from autoscout.pw_precollection import run_pass

ENTRY = """An Invented Coast
Ada Example. Imaginary Press, $28 hardcover (240p)
ISBN 978-0-306-40615-7
Entirely invented review prose for testing only.
Agent: Alice Sample, Example Agency. (Nov.)"""
PAGE = "Nonfiction Reviews\nGeneral Nonfiction\n" + ENTRY
TOKEN = "fixture-subscriber-secret-7c39"
ARCHIVE = f"https://archives.publishersweekly.com/archive/{TOKEN}/12345?cs=email_cover"
TRACKER = (
    "https://sailthru.emagazines.com/click/123/"
    + base64.urlsafe_b64encode(ARCHIVE.encode()).decode().rstrip("=")
    + "/sig"
)
READER = "https://reader.publishersweekly.com/publishers_weekly/20261102/index.html"
PDF = "https://reader.publishersweekly.com/publishers_weekly/20261102/invented.pdf"
HTML = b'<a href="/publishers_weekly/20261102/invented.pdf">PDF</a>'


class ParserTests:
    def test_review_callback_is_opt_in_and_rows_remain_metadata_only(self):
        received = []
        result = parse_pages([PAGE], "2026-11-02", _on_review=lambda row, text: received.append((row, text)))
        assert len(received) == 1
        assert received[0][0] == result.rows[0]
        assert "Entirely invented review prose" in received[0][1]
        assert "Agent:" not in received[0][1]
        assert "Entirely invented review prose" not in json.dumps(result.rows)

    def test_spaced_running_header_starts_nonfiction_section(self):
        page = PAGE.replace("Nonfiction Reviews", "MARCH 30, 2026 PUBLISHERS  WEEKL Y 59Nonfiction Reviews")
        result = parse_pages([page], "2026-03-30")
        assert len(result.rows) == 1

    def test_spaced_other_section_header_ends_nonfiction_section(self):
        child = PAGE.replace("Nonfiction Reviews", "OCTOBER 12, 2026 PUBLISHERS  WEEKL Y 109Children’s & YA Reviews")
        child = child.replace("978-0-306-40615-7", "978-1-4028-9462-6")
        result = parse_pages([PAGE, child], "2026-10-12")
        assert len(result.rows) == 1
        assert result.rows[0]["isbn13"] == "9780306406157"

    def test_plain_star_agent_wrapping_and_photo_credit(self):
        variants = [
            PAGE,
            PAGE.replace("An Invented Coast", "★ An Invented Coast"),
            PAGE.replace("Example. Imaginary Press,", "Example.\nImaginary Press,").replace(
                "hardcover (240p)", "hardcover\n(240p)"
            ),
            PAGE.replace("978-0-306-40615-7", "978-0-306-\n40615-7"),
            PAGE.replace("Ada Example.", "P H OTO © Invented Photographer\nAda Example."),
            PAGE.replace("Ada Example.", "KLAMM © COURTESY OF INVENTED PHOTOGRAPHER\nAda Example."),
        ]
        for index, page in enumerate(variants):
            result = parse_pages([page], "2026-11-02")
            assert result.rejections == {}, (index, result)
            assert len(result.rows) == 1
            row = result.rows[0]
            assert row == {
                "title": "An Invented Coast",
                "primary_authors": ["Ada Example"],
                "publisher": "Imaginary Press",
                "price": "$28",
                "format": "hardcover",
                "page_count": 240,
                "isbn13": "9780306406157",
                "starred": index == 1,
                "subsection": "General Nonfiction",
                "agent": "Alice Sample",
                "agency": "Example Agency",
                "publication_month": "Nov.",
                "issue_date": "2026-11-02",
            }
            assert "prose" not in json.dumps(result.rows)

    @pytest.mark.parametrize(
        "extracted,expected",
        [
            ("/T_h e W orld Is Y ours", "The World Is Yours"),
            ("A T ruth Machine Is T ransforming Math", "A Truth Machine Is Transforming Math"),
            ("Commodi/f_i cation and A/f_t er", "Commodification and After"),
            ("V anishing W omen", "Vanishing Women"),
            ("A to Z of Science", "A to Z of Science"),
        ],
    )
    def test_repairs_pdf_font_artifacts_in_title(self, extracted, expected):
        result = parse_pages([PAGE.replace("An Invented Coast", extracted)], "2026-11-02")
        assert result.rows[0]["title"] == expected

    @pytest.mark.parametrize(
        "extracted,expected",
        [
            ("Kaitlyn Tiff any", "Kaitlyn Tiffany"),
            ("Molly M. Brookfi eld", "Molly M. Brookfield"),
        ],
    )
    def test_repairs_pdf_font_artifacts_in_author_names(self, extracted, expected):
        result = parse_pages([PAGE.replace("Ada Example", extracted)], "2026-11-02")
        assert result.rows[0]["primary_authors"] == [expected]

    def test_multiple_entries_and_subsections(self):
        second = ENTRY.replace("An Invented Coast", "Another Invention").replace(
            "978-0-306-40615-7", "978-1-4028-9462-6"
        )
        result = parse_pages([PAGE + "\nFood & Cooking\n" + second], "2026-11-02")
        assert len(result.rows) == 2
        assert result.rows[1]["subsection"] == "Food & Cooking"

    @pytest.mark.parametrize("header", ["Fiction Reviews", "Children’s & YA Reviews", "No Header"])
    def test_other_sections_never_qualify(self, header):
        assert not parse_pages([PAGE.replace("Nonfiction Reviews", header)], "2026-11-02").rows

    @pytest.mark.parametrize(
        "change,reason",
        [
            (("978-0-306-40615-7", "978-0-306-40615-8"), "pw_isbn_invalid"),
            (("ISBN 978-0-306-40615-7", ""), "pw_isbn_missing"),
            (("Ada Example. Imaginary Press,", "Imaginary Press,"), "pw_author_line_missing_or_unreadable"),
            (("(Nov.)", ""), "pw_entry_unreadable"),
        ],
    )
    def test_named_rejections(self, change, reason):
        result = parse_pages([PAGE.replace(*change)], "2026-11-02")
        assert result.rows == []
        assert result.rejections == {reason: 1}

    @pytest.mark.parametrize("marker", ["PAID REVIEWS", "BOOKLIFE", "self-published"])
    def test_paid_and_self_published_are_counted(self, marker):
        page = PAGE + "\n" + marker
        if marker == "self-published":
            page = PAGE.replace("Imaginary Press", "self-published")
        result = parse_pages([page], "2026-11-02")
        assert not result.rows
        assert result.excluded_paid_self_published == 1

    def test_roles_stay_out_of_primary_authors(self):
        page = PAGE.replace("Ada Example.", "Ada Example and Bert Sample, Trans. Carol Other.")
        result = parse_pages([page], "2026-11-02")
        assert result.rows[0]["primary_authors"] == ["Ada Example", "Bert Sample"]


class Response:
    def __init__(self, url, body):
        self.url, self.body, self.closed = url, io.BytesIO(body), False

    def geturl(self):
        return self.url

    def read(self, size=-1):
        return self.body.read(size)

    def close(self):
        self.closed = True


class Client:
    def __init__(self, reader_url=READER, html=HTML, pdf=b"%PDF-synthetic"):
        self.responses = [Response(reader_url, html), Response(PDF, pdf)]
        self.calls = []

    def open(self, url):
        self.calls.append(url)
        return self.responses[len(self.calls) - 1]


class FetchTests:
    def test_selects_archive_only_not_unsubscribe(self):
        body = f'<a href="https://archives.publishersweekly.com/unsubscribe?ct={TOKEN}">Unsubscribe</a><a href="{TRACKER}">Read</a>'
        assert issue_link(body) == (TRACKER, "12345")
        with pytest.raises(PwError, match="pw_issue_link_missing"):
            issue_link(body.split("</a>")[0])

    def test_a_link_to_an_issue_already_collected_is_ignored(self):
        stale = ARCHIVE.replace("/12345?", "/76746?")
        body = f'<a href="{TRACKER}">Read</a><a href="{stale}">BookLife</a>'
        with pytest.raises(PwError, match="pw_issue_link_missing_or_ambiguous"):
            issue_link(body)
        assert issue_link(body, frozenset({"76746"})) == (TRACKER, "12345")
        # A single link is never dropped; the reader date check refuses a wrong issue.
        assert issue_link(f'<a href="{stale}">Read</a>', frozenset({"76746"}))[1] == "76746"

    def test_pdf_is_deleted_on_success_and_parse_failure(self):
        paths = []

        def reader(path):
            paths.append(path)
            assert path.read_bytes().startswith(b"%PDF")
            return [PAGE]

        client = Client()
        assert fetch_issue(client, TRACKER, "2026-11-02", 100, text_reader=reader) == [PAGE]
        assert client.calls == [TRACKER, PDF]
        assert all(response.closed for response in client.responses)
        assert not paths[0].parent.exists()

        def failing(path):
            paths.append(path)
            raise PwError("pw_pdf_parse_failed")

        with pytest.raises(PwError, match="pw_pdf_parse_failed"):
            fetch_issue(Client(), TRACKER, "2026-11-02", 100, text_reader=failing)
        assert not paths[1].parent.exists()

    @pytest.mark.parametrize(
        "url",
        [
            "https://archives.publishersweekly.com/Account/ExpressLogin",
            "https://unrelated.example/collect",
            "http://reader.publishersweekly.com/test",
        ],
    )
    def test_rejected_destinations(self, url):
        with pytest.raises(PwError, match="pw_subscriber_link_rejected"):
            fetch_issue(Client(reader_url=url), TRACKER, "2026-11-02", 100)
        with pytest.raises(PwError):
            checked_url(url)

    def test_redirect_chain_checked_before_following(self):
        handler = _Redirects()
        req = urllib.request.Request(TRACKER)
        for url in [ARCHIVE, "https://archives.publishersweekly.com/StoryFinderViewIssue/12345", READER]:
            req = handler.redirect_request(req, None, 302, "Found", {}, url)
            assert req.full_url == url
        with pytest.raises(PwError, match="pw_subscriber_link_rejected"):
            handler.redirect_request(
                req, None, 302, "Found", {}, "https://archives.publishersweekly.com/Account/ExpressLogin"
            )

    def test_size_cap_and_date_mismatch(self):
        client = Client(pdf=b"x" * 101)
        with pytest.raises(PwError, match="pw_pdf_too_large"):
            fetch_issue(client, TRACKER, "2026-11-02", 100)
        assert all(r.closed for r in client.responses)
        with pytest.raises(PwError, match="pw_issue_date_mismatch"):
            fetch_issue(Client(), TRACKER, "2026-10-26", 100)

    def test_missing_optional_dependency(self, monkeypatch, tmp_path):
        original_import = builtins.__import__

        def without_reader(name, *args, **kwargs):
            if name == "pypdf":
                raise ImportError
            return original_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", without_reader)
        with pytest.raises(PwError, match="pw_pdf_reader_missing"):
            pdf_page_texts(tmp_path / "unused.pdf")

    def test_real_adapter_with_generated_invented_pdf(self, tmp_path):
        # Optional-extra jobs exercise this; the core suite needs no pypdf.
        pytest.importorskip("pypdf")
        lines = PAGE.encode("ascii").splitlines()
        content = (
            b"BT /F1 10 Tf 50 750 Td "
            + b" 0 -12 Td ".join(b"(" + line.replace(b"(", b"\\(").replace(b")", b"\\)") + b") Tj" for line in lines)
            + b" ET"
        )
        objects = [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream",
        ]
        pdf = b"%PDF-1.4\n"
        offsets = [0]
        for index, obj in enumerate(objects, 1):
            offsets.append(len(pdf))
            pdf += f"{index} 0 obj\n".encode() + obj + b"\nendobj\n"
        start = len(pdf)
        pdf += b"xref\n0 6\n0000000000 65535 f \n" + b"".join(
            f"{offset:010} 00000 n \n".encode() for offset in offsets[1:]
        )
        pdf += f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()
        path = tmp_path / "invented.pdf"
        path.write_bytes(pdf)
        assert len(parse_pages(pdf_page_texts(path), "2026-11-02").rows) == 1

    def test_a_page_past_the_reader_limits_is_marked_not_fatal(self, tmp_path, monkeypatch):
        pypdf = pytest.importorskip("pypdf")
        from autoscout.pw_issue_parser import UNREADABLE_PAGE

        class Page:
            def __init__(self, text):
                self.text = text

            def extract_text(self):
                if self.text is None:
                    raise pypdf.errors.LimitReachedError("invented limit")
                return self.text

        class Reader:
            def __init__(self, path):
                self.pages = [Page(PAGE), Page(None)]

        monkeypatch.setattr(pypdf, "PdfReader", Reader)
        assert pdf_page_texts(tmp_path / "unused.pdf") == [PAGE, UNREADABLE_PAGE]


class Mailbox:
    uidvalidity = 7

    def __init__(self, headers, body):
        self.headers, self.body, self.fetched, self.closed = headers, body, [], False

    def headers_since(self, since, senders):
        return self.headers

    def fetch(self, uid):
        self.fetched.append(uid)
        return self.body

    def close(self):
        self.closed = True


def setup_pass(tmp_path):
    declaration = json.loads(Path("configs/pw-issue-pdf.json").read_text())
    for key in ("collection_file", "attempt_file"):
        declaration[key] = str(tmp_path / (key + ".json"))
    config = tmp_path / "config.json"
    config.write_text(json.dumps(declaration))
    header = MessageHeader(
        4,
        "editor@example.com",
        "Fwd: Your November 2, 2026 Digital Edition Has Arrived",
        datetime(2026, 11, 3, tzinfo=UTC),
    )
    body = f"From: editor@example.com\nContent-Type: text/plain; charset=utf-8\n\nFrom: Publishers Weekly <delivery@mail.emagazines.com>\n{TRACKER}".encode()
    mailbox = Mailbox([header], body)
    return config, declaration, mailbox


CREDENTIALS = {
    "AUTOSCOUT_PM_IMAP_USER": "fixture",
    "AUTOSCOUT_PM_IMAP_PASSWORD": TOKEN,
    "AUTOSCOUT_PW_FORWARDING_SENDER": "editor@example.com",
}


def collect(config, mailbox, **kwargs):
    kwargs.setdefault("environ", CREDENTIALS)
    return run_pass(config, now=datetime(2026, 11, 4, tzinfo=UTC), opener=lambda *args: mailbox, **kwargs)


class CollectionTests:
    def test_result_privacy_and_already_collected_issue(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        report = collect(config, mailbox, client=Client(), text_reader=lambda path: [PAGE])
        assert mailbox.closed
        assert report["nonfiction_rows"] == 1
        result = json.loads(Path(declaration["collection_file"]).read_text())
        assert result["issues"] == [{"issue_date": "2026-11-02", "issue_id": "12345"}]
        for key in ("collection_file", "attempt_file"):
            saved = Path(declaration[key]).read_text()
            assert TOKEN not in saved and TRACKER not in saved and "prose" not in saved
        assert TOKEN not in json.dumps(report)
        # A new forwarded copy of the same issue must not even follow the link.
        mailbox.headers = [
            MessageHeader(5, mailbox.headers[0].sender, mailbox.headers[0].subject, mailbox.headers[0].date)
        ]
        client = Client()
        second = collect(config, mailbox, client=client)
        assert second["status"] == "no_new_issue"
        assert client.calls == []
        assert mailbox.fetched == [4]

    def test_failure_keeps_no_rows_marks_a_retry_and_sanitizes_attempt(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        with pytest.raises(PwError, match="pw_zero_nonfiction_rows"):
            collect(config, mailbox, client=Client(), text_reader=lambda path: ["Fiction Reviews"])
        saved = json.loads(Path(declaration["collection_file"]).read_text())
        assert (saved["rows"], saved["issues"], saved["retry_issues"]) == ([], [], ["2026-11-02"])
        assert json.loads(Path(declaration["attempt_file"]).read_text())["reason_code"] == "pw_zero_nonfiction_rows"
        assert mailbox.closed

    def test_original_sender_required(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        mailbox.body = mailbox.body.replace(b"delivery@mail.emagazines.com", b"someone@example.org")
        with pytest.raises(PwError, match="pw_original_sender_missing"):
            collect(config, mailbox)

    def test_missing_credential_does_not_open_mailbox(self, tmp_path):
        config, _, mailbox = setup_pass(tmp_path)
        with pytest.raises(PwError, match="pw_credentials_missing"):
            run_pass(config, environ={}, key_file=tmp_path / "missing", opener=lambda *args: pytest.fail("opened"))

    def test_forwarding_sender_is_read_from_the_environment_or_key_file(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        bare = {"AUTOSCOUT_PM_IMAP_USER": "fixture", "AUTOSCOUT_PM_IMAP_PASSWORD": TOKEN}
        # Without the variable only the configured senders are searched, so the forward is not read.
        unset = collect(config, mailbox, environ=bare, key_file=tmp_path / "missing.env")
        assert unset["status"] == "no_new_issue"
        assert mailbox.fetched == []
        # The same forward is read once the operator's key file names the sender.
        key_file = tmp_path / "pm-mailbox.env"
        key_file.write_text("AUTOSCOUT_PW_FORWARDING_SENDER=Editor@Example.com\n", encoding="utf-8")
        report = collect(
            config, mailbox, client=Client(), text_reader=lambda path: [PAGE], environ=bare, key_file=key_file
        )
        assert report["status"] == "collected"
        assert report["issues_collected"] == ["2026-11-02"]

    def test_forwarding_sender_must_be_an_address(self, tmp_path):
        config, _, mailbox = setup_pass(tmp_path)
        environ = {**CREDENTIALS, "AUTOSCOUT_PW_FORWARDING_SENDER": "not-an-address"}
        with pytest.raises(PwError, match="pw_declaration_invalid") as raised:
            collect(config, mailbox, environ=environ)
        assert "not-an-address" not in str(raised.value)
        assert mailbox.fetched == []

    def test_uncollected_issues_oldest_first_within_the_age_window(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        mailbox.headers += [
            issue_header(8, "October 26, 2026", datetime(2026, 10, 27, tzinfo=UTC)),
            MessageHeader(5, "editor@example.com", "Unrelated", datetime(2026, 11, 3, tzinfo=UTC)),
            issue_header(6, "November 2, 2025", datetime(2025, 11, 3, tzinfo=UTC)),
        ]
        client = IssueClient(["20261026", "20261102"])
        report = collect(config, mailbox, client=client, text_reader=numbered_pages())
        assert mailbox.fetched == [8, 4]
        assert report["status"] == "collected"
        assert report["issues_collected"] == ["2026-10-26", "2026-11-02"]
        assert (report["emails_read"], report["emails_skipped"], report["nonfiction_rows"]) == (2, 1, 2)
        saved = json.loads(Path(declaration["collection_file"]).read_text())
        assert [issue["issue_date"] for issue in saved["issues"]] == ["2026-10-26", "2026-11-02"]


def issue_header(uid, day, received):
    return MessageHeader(uid, "editor@example.com", f"Fwd: Your {day} Digital Edition Has Arrived", received)


class IssueClient:
    """A subscriber session per issue: each archive link reaches the next listed reader stamp."""

    def __init__(self, stamps, fail=None):
        self.stamps, self.fail, self.calls = list(stamps), fail or {}, []

    def open(self, url):
        self.calls.append(url)
        if url.endswith(".pdf"):
            return Response(url, b"%PDF-synthetic")
        stamp = self.stamps.pop(0)
        if stamp in self.fail:
            raise self.fail[stamp]
        return Response(
            f"https://reader.publishersweekly.com/publishers_weekly/{stamp}/index.html",
            f'<a href="/publishers_weekly/{stamp}/invented.pdf">PDF</a>'.encode(),
        )


ISBNS = ["978-0-306-40615-7", "978-1-4028-9462-6", "978-0-596-52068-7", "978-1-86197-876-9", "978-3-16-148410-0"]


def numbered_pages():
    count = iter(ISBNS)
    return lambda path: [PAGE.replace("978-0-306-40615-7", next(count))]


class BackfillTests:
    """#963: every uncollected issue is collected, oldest first, each saved on its own."""

    OLD = (
        issue_header(1, "September 28, 2026", datetime(2026, 9, 29, tzinfo=UTC)),
        issue_header(2, "October 5, 2026", datetime(2026, 10, 6, tzinfo=UTC)),
        issue_header(3, "October 12, 2026", datetime(2026, 10, 13, tzinfo=UTC)),
    )

    def test_emails_older_than_the_window_need_a_backfill(self, tmp_path):
        config, _, mailbox = setup_pass(tmp_path)
        mailbox.headers = [self.OLD[0]]
        assert collect(config, mailbox, client=IssueClient([]))["status"] == "no_new_issue"
        report = collect(config, mailbox, backfill=True, client=IssueClient(["20260928"]), text_reader=numbered_pages())
        assert report["issues_collected"] == ["2026-09-28"]

    def test_a_backfill_reads_every_issue_and_the_next_pass_reads_none(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        mailbox.headers += self.OLD
        stamps = ["20260928", "20261005", "20261012", "20261102"]
        report = collect(config, mailbox, backfill=True, client=IssueClient(stamps), text_reader=numbered_pages())
        assert report["issues_collected"] == ["2026-09-28", "2026-10-05", "2026-10-12", "2026-11-02"]
        assert report["nonfiction_rows"] == 4
        assert len(json.loads(Path(declaration["collection_file"]).read_text())["rows"]) == 4
        client = IssueClient([])
        assert collect(config, mailbox, backfill=True, client=client)["status"] == "no_new_issue"
        assert client.calls == []

    def test_more_issues_than_the_pass_limit_wait_for_the_next_pass(self, tmp_path):
        config, _, mailbox = setup_pass(tmp_path)
        mailbox.headers += [*self.OLD, issue_header(9, "September 21, 2026", datetime(2026, 9, 22, tzinfo=UTC))]
        pages = numbered_pages()
        first = collect(
            config,
            mailbox,
            backfill=True,
            client=IssueClient(["20260921", "20260928", "20261005", "20261012"]),
            text_reader=pages,
        )
        assert (first["issues_collected"][0], first["issues_pending"]) == ("2026-09-21", 1)
        second = collect(config, mailbox, backfill=True, client=IssueClient(["20261102"]), text_reader=pages)
        assert (second["issues_collected"], second["issues_pending"]) == (["2026-11-02"], 0)

    def test_two_copies_of_one_issue_are_read_once(self, tmp_path):
        config, _, mailbox = setup_pass(tmp_path)
        mailbox.headers.append(
            MessageHeader(
                7,
                "delivery@mail.emagazines.com",
                "Your November 2, 2026 Digital Edition Has Arrived",
                datetime(2026, 11, 2, tzinfo=UTC),
            )
        )
        report = collect(config, mailbox, client=IssueClient(["20261102"]), text_reader=numbered_pages())
        assert mailbox.fetched == [7]
        assert (report["emails_read"], report["emails_skipped"]) == (1, 1)

    def test_a_booklife_email_with_the_same_subject_is_ignored(self, tmp_path):
        config, _, mailbox = setup_pass(tmp_path)
        pw = mailbox.headers[0]
        mailbox.headers.append(
            MessageHeader(8, pw.sender, pw.subject, datetime(2026, 11, 4, tzinfo=UTC), sender_name="BookLife")
        )
        report = collect(config, mailbox, client=IssueClient(["20261102"]), text_reader=numbered_pages())
        assert mailbox.fetched == [4]
        assert report["issues_collected"] == ["2026-11-02"]
        assert report["emails_skipped"] == 1

    def test_a_stale_link_to_a_collected_issue_beside_the_current_one(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        stale = ARCHIVE.replace("/12345?", "/76746?")
        mailbox.body += f"\n{stale}".encode()
        with pytest.raises(PwError, match="^pw_issue_link_missing_or_ambiguous$"):
            collect(config, mailbox, client=IssueClient([]))
        result = Path(declaration["collection_file"])
        saved = json.loads(result.read_text())
        assert saved["skipped_issues"] == [
            {"issue_date": "2026-11-02", "reason_code": "pw_issue_link_missing_or_ambiguous", "link_rule": 2}
        ]
        # Not retried under the same rule.
        client = IssueClient([])
        assert collect(config, mailbox, client=client)["status"] == "no_new_issue"
        assert client.calls == []
        # Once the stale issue is collected, a skip recorded under the old rule is retried.
        saved["issues"] = [{"issue_date": "2026-10-26", "issue_id": "76746"}]
        saved["skipped_issues"] = [{"issue_date": "2026-11-02", "reason_code": "pw_issue_link_missing_or_ambiguous"}]
        result.write_text(json.dumps(saved))
        report = collect(config, mailbox, client=IssueClient(["20261102"]), text_reader=numbered_pages())
        assert report["issues_collected"] == ["2026-11-02"]
        assert json.loads(result.read_text())["skipped_issues"] == []

    def test_a_transient_failure_keeps_the_others_and_is_retried(self, tmp_path):
        import urllib.error

        config, declaration, mailbox = setup_pass(tmp_path)
        mailbox.headers += self.OLD[:1]
        client = IssueClient(["20260928", "20261102"], fail={"20260928": urllib.error.URLError("down")})
        report = collect(config, mailbox, backfill=True, client=client, text_reader=numbered_pages())
        assert report["status"] == "partial"
        assert report["issues_collected"] == ["2026-11-02"]
        assert report["issues_failed"] == [{"issue_date": "2026-09-28", "reason_code": "pw_download_failed"}]
        assert json.loads(Path(declaration["collection_file"]).read_text())["skipped_issues"] == []
        retry = collect(config, mailbox, backfill=True, client=IssueClient(["20260928"]), text_reader=numbered_pages())
        assert retry["issues_collected"] == ["2026-09-28"]

    def test_a_permanent_failure_is_recorded_once_and_not_retried(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        mailbox.headers = self.OLD[:1]
        with pytest.raises(PwError, match="^pw_issue_date_mismatch$"):
            collect(config, mailbox, backfill=True, client=IssueClient(["20261102"]))
        saved = json.loads(Path(declaration["collection_file"]).read_text())
        assert saved["skipped_issues"] == [{"issue_date": "2026-09-28", "reason_code": "pw_issue_date_mismatch"}]
        client = IssueClient([])
        assert collect(config, mailbox, backfill=True, client=client)["status"] == "no_new_issue"
        assert client.calls == []

    def test_an_issue_that_keeps_failing_goes_after_new_ones(self, tmp_path):
        import urllib.error

        config, _, mailbox = setup_pass(tmp_path)
        mailbox.headers = [*self.OLD, issue_header(9, "September 21, 2026", datetime(2026, 9, 22, tzinfo=UTC))]
        down = urllib.error.URLError("down")
        first = collect(
            config,
            mailbox,
            backfill=True,
            client=IssueClient(["20260921", "20260928", "20261005", "20261012"], fail={"20260921": down}),
            text_reader=numbered_pages(),
        )
        assert first["issues_failed"] == [{"issue_date": "2026-09-21", "reason_code": "pw_download_failed"}]
        # The next pass reaches the new issue first even though the failed one is older.
        mailbox.headers.append(issue_header(10, "October 19, 2026", datetime(2026, 10, 20, tzinfo=UTC)))
        second = collect(
            config, mailbox, backfill=True, client=IssueClient(["20261019", "20260921"]), text_reader=numbered_pages()
        )
        assert second["issues_collected"] == ["2026-10-19", "2026-09-21"]

    def test_an_issue_with_an_unreadable_review_page_is_skipped_by_name(self, tmp_path):
        from autoscout.pw_issue_parser import UNREADABLE_PAGE

        config, declaration, mailbox = setup_pass(tmp_path)
        mailbox.headers = self.OLD[:1]
        reader = lambda path: [PAGE, UNREADABLE_PAGE]  # noqa: E731
        with pytest.raises(PwError, match="^pw_page_unreadable$"):
            collect(config, mailbox, backfill=True, client=IssueClient(["20260928"]), text_reader=reader)
        saved = json.loads(Path(declaration["collection_file"]).read_text())
        assert saved["rows"] == []
        assert saved["skipped_issues"] == [{"issue_date": "2026-09-28", "reason_code": "pw_page_unreadable"}]

    def test_an_unreadable_email_fails_its_issue_alone(self, tmp_path):
        from autoscout.pm_mailbox import REASON_PROTOCOL
        from autoscout.pm_mailbox import PmMailboxError

        config, _, mailbox = setup_pass(tmp_path)
        mailbox.headers += self.OLD[:1]
        fetch = mailbox.fetch

        def flaky(uid):
            if uid == 1:
                raise PmMailboxError(REASON_PROTOCOL, "gone")
            return fetch(uid)

        mailbox.fetch = flaky
        report = collect(config, mailbox, backfill=True, client=IssueClient(["20261102"]), text_reader=numbered_pages())
        assert report["issues_collected"] == ["2026-11-02"]
        assert report["issues_failed"] == [{"issue_date": "2026-09-28", "reason_code": "pw_imap_error"}]
        assert report["emails_read"] == 1

    @pytest.mark.parametrize(
        "error,reason", [(TimeoutError(), "pw_timeout"), (ConnectionResetError(), "pw_connection_failed")]
    )
    def test_a_transport_error_on_one_email_fails_its_issue_alone(self, tmp_path, error, reason):
        config, _, mailbox = setup_pass(tmp_path)
        mailbox.headers += self.OLD[:1]
        fetch = mailbox.fetch

        def flaky(uid):
            if uid == 4:
                raise error
            return fetch(uid)

        mailbox.fetch = flaky
        report = collect(config, mailbox, backfill=True, client=IssueClient(["20260928"]), text_reader=numbered_pages())
        assert report["issues_collected"] == ["2026-09-28"]
        assert report["issues_failed"] == [{"issue_date": "2026-11-02", "reason_code": reason}]

    def test_an_older_issue_never_replaces_a_newer_review_of_the_same_book(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        assert (
            collect(config, mailbox, client=IssueClient(["20261102"]), text_reader=lambda path: [PAGE])["status"]
            == "collected"
        )
        mailbox.headers = self.OLD[:1]
        older = PAGE.replace("An Invented Coast", "An Earlier Coast")
        collect(config, mailbox, backfill=True, client=IssueClient(["20260928"]), text_reader=lambda path: [older])
        (row,) = json.loads(Path(declaration["collection_file"]).read_text())["rows"]
        assert (row["title"], row["issue_date"]) == ("An Invented Coast", "2026-11-02")

    @pytest.mark.parametrize(
        "link",
        ["https://archives.publishersweekly.com:bad/archive/x/1", "https://[archives.publishersweekly.com/archive/x/1"],
    )
    def test_a_malformed_link_fails_its_issue_alone(self, tmp_path, link):
        config, _, mailbox = setup_pass(tmp_path)
        mailbox.headers += self.OLD[:1]
        good, fetch = mailbox.body, mailbox.fetch
        mailbox.fetch = lambda uid: good.replace(TRACKER.encode(), link.encode()) if uid == 1 else fetch(uid)
        report = collect(config, mailbox, backfill=True, client=IssueClient(["20261102"]), text_reader=numbered_pages())
        assert report["issues_collected"] == ["2026-11-02"]
        assert [failure["issue_date"] for failure in report["issues_failed"]] == ["2026-09-28"]

    def test_a_backfill_failure_is_retried_by_an_ordinary_pass(self, tmp_path):
        import urllib.error

        config, _, mailbox = setup_pass(tmp_path)
        mailbox.headers = self.OLD[:1]
        down = {"20260928": urllib.error.URLError("down")}
        with pytest.raises(PwError, match="^pw_download_failed$"):
            collect(config, mailbox, backfill=True, client=IssueClient(["20260928"], fail=down))
        retry = collect(config, mailbox, client=IssueClient(["20260928"]), text_reader=numbered_pages())
        assert retry["issues_collected"] == ["2026-09-28"]
        # Collected, it is no longer reachable outside the window.
        assert collect(config, mailbox, client=IssueClient([]))["status"] == "no_new_issue"

    def test_retries_rotate_so_every_failed_issue_gets_a_turn(self, tmp_path):
        import urllib.error

        config, declaration, mailbox = setup_pass(tmp_path)
        days = [21, 22, 23, 24, 25]
        mailbox.headers = [
            issue_header(day, f"September {day}, 2026", datetime(2026, 9, day + 1, tzinfo=UTC)) for day in days
        ]
        stamps = [f"202609{day}" for day in days]
        down = {stamp: urllib.error.URLError("down") for stamp in stamps}

        def failed_pass(client):
            with pytest.raises(PwError, match="^pw_download_failed$"):
                collect(config, mailbox, backfill=True, client=client)
            attempt = json.loads(Path(declaration["attempt_file"]).read_text())
            return [failure["issue_date"] for failure in attempt["issues_failed"]]

        assert failed_pass(IssueClient(stamps[:4], fail=down)) == [
            "2026-09-21",
            "2026-09-22",
            "2026-09-23",
            "2026-09-24",
        ]
        # Never tried yet, the 25th now leads; the retries follow, longest-waiting first.
        second = failed_pass(IssueClient(["20260925", *stamps[:3]], fail=down))
        assert second == ["2026-09-25", "2026-09-21", "2026-09-22", "2026-09-23"]
        saved = json.loads(Path(declaration["collection_file"]).read_text())
        assert saved["retry_issues"] == ["2026-09-24", "2026-09-25", "2026-09-21", "2026-09-22", "2026-09-23"]
        # The 24th, stuck at the back before, now comes first and its download works again.
        still_down = {stamp: down[stamp] for stamp in ("20260925", "20260921", "20260922")}
        third = collect(
            config,
            mailbox,
            backfill=True,
            client=IssueClient(["20260924", "20260925", "20260921", "20260922"], fail=still_down),
            text_reader=numbered_pages(),
        )
        assert (third["status"], third["issues_collected"]) == ("partial", ["2026-09-24"])

    @pytest.mark.parametrize("recorded", [None, 150_000_000])
    def test_an_over_cap_issue_is_retried_once_the_cap_rises(self, tmp_path, recorded):
        config, declaration, mailbox = setup_pass(tmp_path)
        mailbox.headers = self.OLD[:1]
        skip = {"issue_date": "2026-09-28", "reason_code": "pw_pdf_too_large"}
        if recorded is not None:
            skip["max_pdf_bytes"] = recorded
        result = Path(declaration["collection_file"])
        result.write_text(json.dumps({"schema_version": 1, "rows": [], "issues": [], "skipped_issues": [skip]}))
        # An ordinary pass reaches it even though its email is outside the window.
        report = collect(config, mailbox, client=IssueClient(["20260928"]), text_reader=numbered_pages())
        assert report["issues_collected"] == ["2026-09-28"]
        assert json.loads(result.read_text())["skipped_issues"] == []

    def test_an_over_cap_issue_records_its_cap_and_is_not_retried_under_it(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        mailbox.headers = self.OLD[:1]
        big = Client(reader_url=READER.replace("20261102", "20260928"), html=HTML.replace(b"20261102", b"20260928"))
        big.responses[1] = Response(PDF.replace("20261102", "20260928"), b"x" * 101)
        declaration["max_pdf_bytes"] = 100
        config.write_text(json.dumps(declaration))
        with pytest.raises(PwError, match="^pw_pdf_too_large$"):
            collect(config, mailbox, backfill=True, client=big)
        saved = json.loads(Path(declaration["collection_file"]).read_text())
        assert saved["skipped_issues"] == [
            {"issue_date": "2026-09-28", "reason_code": "pw_pdf_too_large", "max_pdf_bytes": 100}
        ]
        client = IssueClient([])
        assert collect(config, mailbox, backfill=True, client=client)["status"] == "no_new_issue"
        assert client.calls == []

    def test_a_rejected_issue_still_reports_its_counts(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        invalid = PAGE.replace("978-0-306-40615-7", "978-0-306-40615-8")
        with pytest.raises(PwError, match="^pw_zero_nonfiction_rows$"):
            collect(config, mailbox, client=Client(), text_reader=lambda path: [invalid])
        attempt = json.loads(Path(declaration["attempt_file"]).read_text())
        assert (attempt["pages_read"], attempt["rejections"]) == (1, {"pw_isbn_invalid": 1})

    def test_a_missing_pdf_reader_stops_the_pass(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        mailbox.headers += self.OLD[:1]

        def missing(path):
            raise PwError("pw_pdf_reader_missing")

        with pytest.raises(PwError, match="^pw_pdf_reader_missing$"):
            collect(config, mailbox, backfill=True, client=IssueClient(["20260928", "20261102"]), text_reader=missing)
        assert not Path(declaration["collection_file"]).exists()


class AdditionalBoundaryTests:
    @pytest.mark.parametrize("noise", ["“A pull quote from the review”", "ADVERTISEMENT", "BUY NOW"])
    def test_noisy_metadata_is_rejected_instead_of_retained(self, noise):
        parsed = parse_pages([PAGE.replace("Ada Example.", noise + "\nAda Example.")], "2026-11-02")
        assert not parsed.rows
        assert parsed.rejections == {"pw_author_line_missing_or_unreadable": 1}

    def test_public_output_exposes_no_transport_exception(self, tmp_path, monkeypatch, capsys):
        from autoscout import pw_precollection

        def fail(*args, **kwargs):
            raise PwError("pw_subscriber_link_rejected")

        monkeypatch.setattr(pw_precollection, "run_pass", fail)
        assert pw_precollection.collect_pw_issue_command(tmp_path / "unused") == 2
        output = capsys.readouterr()
        assert TOKEN not in output.out + output.err
        assert json.loads(output.out)["reason_code"] == "pw_subscriber_link_rejected"

    def test_download_exception_has_named_sanitized_failure(self):
        class FailedClient:
            def open(self, url):
                raise OSError(f"secret URL {TRACKER}")

        with pytest.raises(PwError, match="^pw_download_failed$"):
            fetch_issue(FailedClient(), TRACKER, "2026-11-02", 100)

    def test_direct_delivery(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        original = mailbox.headers[0]
        mailbox.headers = [
            MessageHeader(1, "delivery@mail.emagazines.com", original.subject.removeprefix("Fwd: "), original.date)
        ]
        mailbox.body = f"From: delivery@mail.emagazines.com\nContent-Type: text/plain\n\n{TRACKER}".encode()
        report = collect(config, mailbox, client=Client(), text_reader=lambda path: [PAGE])
        assert report["nonfiction_rows"] == 1
        assert mailbox.fetched == [1]

    def test_failure_keeps_existing_rows_and_issues(self, tmp_path):
        config, declaration, mailbox = setup_pass(tmp_path)
        result = Path(declaration["collection_file"])
        row = {"isbn13": "9781402894626", "issue_date": "2026-10-26"}
        result.write_text(
            json.dumps({"schema_version": 1, "rows": [row], "issues": [{"issue_date": "2026-10-26", "issue_id": "1"}]})
        )
        with pytest.raises(PwError):
            collect(
                config, mailbox, client=Client(reader_url="https://archives.publishersweekly.com/Account/ExpressLogin")
            )
        saved = json.loads(result.read_text())
        assert (saved["rows"], saved["issues"]) == ([row], [{"issue_date": "2026-10-26", "issue_id": "1"}])
        assert TOKEN not in Path(declaration["attempt_file"]).read_text()


class ObservedStructureTests:
    def test_glued_running_header_and_ligatures_with_continuation(self):
        # Structure observed live, words invented. No original review is retained.
        page = PAGE.replace("Nonfiction Reviews", "NOVEMBER 2, 2026 PUBLISHERS WEEKLY 47Nonﬁction Reviews")
        page = page.replace("General Nonfiction\n", "An invented end of the preceding review. (Oct.)\n")
        parsed = parse_pages([page], "2026-11-02")
        assert len(parsed.rows) == 1
        assert parsed.rows[0]["title"] == "An Invented Coast"

    def test_initials_do_not_become_the_publisher(self):
        parsed = parse_pages([PAGE.replace("Ada Example.", "A. Q. Example.")], "2026-11-02")
        assert parsed.rows[0]["primary_authors"] == ["A. Q. Example"]
        assert parsed.rows[0]["publisher"] == "Imaginary Press"

    def test_wrapped_agent_and_agency(self):
        parsed = parse_pages(
            [PAGE.replace("Alice Sample, Example Agency.", "Alice\nSample, Example\nAgency.")], "2026-11-02"
        )
        assert parsed.rows[0]["agent"] == "Alice Sample"
        assert parsed.rows[0]["agency"] == "Example Agency"

    def test_general_reviews_header_alone_is_not_enough_to_guess_nonfiction(self):
        opener = PAGE.replace("Nonfiction Reviews\nGeneral Nonfiction\n", "Reviews\n")
        assert not parse_pages([opener], "2026-11-02").rows


class SectionContinuationTests:
    def test_bounded_continuation_restores_cross_page_review_endings(self):
        first, end = PAGE.rsplit("Agent:", 1)
        second = (
            "Agent:"
            + end
            + "\n"
            + ENTRY.replace("An Invented Coast", "A Second Invention").replace("978-0-306-40615-7", "978-1-4028-9462-6")
        )
        third = "Children’s & YA Reviews\n" + ENTRY.replace("978-0-306-40615-7", "978-0-7432-7356-5")
        parsed = parse_pages([first, second, third, ENTRY], "2026-11-02")
        assert [row["isbn13"] for row in parsed.rows] == ["9780306406157", "9781402894626"]
        assert not parsed.rejections

    @pytest.mark.parametrize("boundary", ["Fiction Reviews", "Reviews", "PAID REVIEWS", "BookLifeReviews"])
    def test_every_new_section_closes_the_nonfiction_block(self, boundary):
        extra = ENTRY.replace("978-0-306-40615-7", "978-1-4028-9462-6")
        parsed = parse_pages([PAGE, boundary + "\n" + extra, extra], "2026-11-02")
        assert len(parsed.rows) == 1

    def test_unclassified_pages_before_the_first_explicit_header_stay_out(self):
        parsed = parse_pages([ENTRY, PAGE], "2026-11-02")
        assert len(parsed.rows) == 1

    def test_subsection_furniture_case_and_spacing_are_normalized(self):
        parsed = parse_pages([PAGE.replace("General Nonfiction", "HEAL TH, HOME & HOBBIES")], "2026-11-02")
        assert parsed.rows[0]["subsection"] == "Health, Home & Hobbies"


class SectionOpenerTests:
    """A section opener prints only the generic "Reviews" logo (observed 2 Nov 2026)."""

    OPENED = "Reviews\n" + ENTRY.replace("An Invented Coast", "An Opening Invention").replace(
        "978-0-306-40615-7", "978-0-596-52068-7"
    )
    CONTINUED = ENTRY.replace("An Invented Coast", "A Continued Invention").replace(
        "978-0-306-40615-7", "978-1-86197-876-9"
    )

    def test_opener_takes_the_section_of_the_next_headed_page(self):
        parsed = parse_pages([self.OPENED, PAGE], "2026-11-02")
        assert [row["isbn13"] for row in parsed.rows] == ["9780596520687", "9780306406157"]
        assert not parsed.rejections
        assert parsed.rows[0]["title"] == "An Opening Invention"

    def test_opener_reaches_a_header_two_pages_ahead(self):
        parsed = parse_pages([self.OPENED, self.CONTINUED, PAGE], "2026-11-02")
        assert [row["isbn13"] for row in parsed.rows] == ["9780596520687", "9781861978769", "9780306406157"]

    def test_opener_beyond_the_lookahead_bound_stays_out(self):
        later = self.CONTINUED.replace("978-1-86197-876-9", "978-3-16-148410-0")
        parsed = parse_pages([self.OPENED, self.CONTINUED, later, PAGE], "2026-11-02")
        assert [row["isbn13"] for row in parsed.rows] == ["9780306406157"]

    @pytest.mark.parametrize("header", ["Fiction Reviews", "Children’s & YA Reviews"])
    def test_opener_of_another_section_stays_out(self, header):
        other = header + "\n" + self.CONTINUED
        parsed = parse_pages([self.OPENED, other], "2026-11-02")
        assert not parsed.rows

    def test_a_paid_page_before_the_header_keeps_the_opener_out(self):
        parsed = parse_pages([self.OPENED, "PAID REVIEWS\n" + self.CONTINUED, PAGE], "2026-11-02")
        assert [row["isbn13"] for row in parsed.rows] == ["9780306406157"]

    def test_only_the_opener_next_to_the_header_opens_the_section(self):
        parsed = parse_pages([self.OPENED, "Reviews\n" + self.CONTINUED, PAGE], "2026-11-02")
        assert [row["isbn13"] for row in parsed.rows] == ["9781861978769", "9780306406157"]

    def test_next_opener_closes_the_nonfiction_block(self):
        children = "Children’s & YA Reviews\n" + self.CONTINUED
        parsed = parse_pages([PAGE, self.OPENED, children], "2026-11-02")
        assert [row["isbn13"] for row in parsed.rows] == ["9780306406157"]


class WrappedEntryTests:
    """Layouts found by review on #959; all names and prose are invented."""

    SECOND = ENTRY.replace("An Invented Coast", "A Second Invention").replace("978-0-306-40615-7", "978-1-4028-9462-6")

    @pytest.mark.parametrize(
        "author_lines,authors",
        [
            ("Ada Example and\nBert Sample.", ["Ada Example", "Bert Sample"]),
            ("Ada Example, Bert Sample,\nand Carol Other.", ["Ada Example", "Bert Sample", "Carol Other"]),
            ("Ada Example, trans. from the Invented\nby Bert Sample.", ["Ada Example"]),
            ("Ada Example, illus. by Bert\nSample.", ["Ada Example"]),
        ],
    )
    def test_a_wrapped_author_line_stays_whole(self, author_lines, authors):
        parsed = parse_pages([PAGE.replace("Ada Example.", author_lines)], "2026-11-02")
        assert parsed.rows[0]["title"] == "An Invented Coast"
        assert parsed.rows[0]["primary_authors"] == authors

    def test_a_wrapped_title_still_ends_before_the_author(self):
        page = PAGE.replace("An Invented Coast", "An Invented Coast: Tides\nof the Imaginary Sea")
        parsed = parse_pages([page], "2026-11-02")
        assert parsed.rows[0]["title"] == "An Invented Coast: Tides of the Imaginary Sea"
        assert parsed.rows[0]["primary_authors"] == ["Ada Example"]

    @pytest.mark.parametrize("prose", ["2026 was an invented year.", "100 invented ways are explored."])
    def test_numbers_opening_the_review_stay_out_of_the_isbn(self, prose):
        parsed = parse_pages([PAGE.replace("Entirely invented review prose for testing only.", prose)], "2026-11-02")
        assert parsed.rejections == {}
        assert parsed.rows[0]["isbn13"] == "9780306406157"

    @pytest.mark.parametrize("position", ["first", "middle", "last"])
    def test_an_entry_without_an_isbn_is_counted_next_to_good_ones(self, position):
        missing = ENTRY.replace("An Invented Coast", "A Lost Invention").replace("ISBN 978-0-306-40615-7\n", "")
        entries = {
            "first": [missing, ENTRY, self.SECOND],
            "middle": [ENTRY, missing, self.SECOND],
            "last": [ENTRY, self.SECOND, missing],
        }[position]
        parsed = parse_pages(["Nonfiction Reviews\n" + "\n".join(entries)], "2026-11-02")
        assert len(parsed.rows) == 2
        assert parsed.rejections == {"pw_isbn_missing": 1}


class EntryBoundaryTests:
    """Round 2 review layouts on #959; all names and prose are invented."""

    NEXT = ENTRY.replace("An Invented Coast", "A Neighbouring Invention").replace(
        "978-0-306-40615-7", "978-1-4028-9462-6"
    )

    def test_a_name_wrapped_mid_way_is_rejected_not_split(self):
        parsed = parse_pages([PAGE.replace("Ada Example.", "Ada Example and Bert\nSample.")], "2026-11-02")
        assert parsed.rows == []
        assert parsed.rejections == {"pw_author_line_missing_or_unreadable": 1}

    def test_prose_about_paid_reviews_does_not_make_a_paid_page(self):
        prose = "This invented history examines paid reviews and BookLife-style programs."
        parsed = parse_pages([PAGE.replace("Entirely invented review prose for testing only.", prose)], "2026-11-02")
        assert len(parsed.rows) == 1
        assert parsed.excluded_paid_self_published == 0

    def test_prose_about_self_publishing_keeps_the_book(self):
        prose = "The author revisits her self-published debut."
        parsed = parse_pages([PAGE.replace("Entirely invented review prose for testing only.", prose)], "2026-11-02")
        assert len(parsed.rows) == 1
        assert parsed.excluded_paid_self_published == 0

    @pytest.mark.parametrize("identifier", ["ISBN 978-0-596-52068-7", "ASIN B0INVENTED1"])
    def test_a_self_published_ending_closes_its_entry_and_keeps_the_next(self, identifier):
        self_published = (
            "An Independent Invention\nCara Sample. Cara Sample, $4.99 e-book (300p)\n"
            + identifier
            + "\nInvented prose about an invented war. (Self-published)"
        )
        parsed = parse_pages(["Nonfiction Reviews\n" + self_published + "\n" + self.NEXT], "2026-11-02")
        assert [row["title"] for row in parsed.rows] == ["A Neighbouring Invention"]
        assert parsed.rejections == {}
        assert parsed.excluded_paid_self_published == 1


class RoundThreeTests:
    """Round 3 review cases on #959; invented text only."""

    def test_an_overlong_isbn_is_invalid_not_truncated(self):
        parsed = parse_pages([PAGE.replace("978-0-306-40615-7", "978-0-306-40615-70")], "2026-11-02")
        assert parsed.rows == []
        assert parsed.rejections == {"pw_isbn_invalid": 1}

    def test_text_between_the_agent_and_the_month_drops_the_agent_only(self):
        parsed = parse_pages([PAGE.replace("Example Agency. (Nov.)", "Example Agency.\nBUY NOW. (Nov.)")], "2026-11-02")
        assert parsed.rows[0]["title"] == "An Invented Coast"
        assert (parsed.rows[0]["agent"], parsed.rows[0]["agency"]) == ("", "")

    def test_a_truncated_body_is_a_named_download_failure(self):
        import http.client

        class TruncatedClient:
            def open(self, url):
                raise http.client.IncompleteRead(b"partial")

        with pytest.raises(PwError, match="^pw_download_failed$"):
            fetch_issue(TruncatedClient(), TRACKER, "2026-11-02", 100)


class HeaderlessSectionTests:
    """12 and 19 Oct 2026 print no running header on any review page; invented text only."""

    OPENER = "Reviews\nFOOD & COOKING\n" + ENTRY
    NEXT = ENTRY.replace("An Invented Coast", "A Later Invention").replace("978-0-306-40615-7", "978-1-4028-9462-6")

    def test_nonfiction_subsections_classify_a_headerless_block(self):
        parsed = parse_pages([self.OPENER, self.NEXT], "2026-10-19")
        assert [row["isbn13"] for row in parsed.rows] == ["9780306406157", "9781402894626"]
        assert parsed.rows[0]["subsection"] == "Food & Cooking"

    def test_a_nonfiction_heading_on_a_later_page_of_the_block_counts(self):
        opener = "Reviews\n" + ENTRY
        parsed = parse_pages([opener, "RELIGION/SPIRITUALITY\n" + self.NEXT], "2026-10-12")
        assert len(parsed.rows) == 2

    @pytest.mark.parametrize("heading", ["MYSTERIES & THRILLERS", "PICTURE BOOKS", "FICTION"])
    def test_any_other_section_heading_keeps_the_block_out(self, heading):
        parsed = parse_pages([self.OPENER, heading + "\n" + self.NEXT], "2026-10-19")
        assert not parsed.rows

    @pytest.mark.parametrize("heading", ["COMICS", "LIFESTYLE"])
    def test_headings_shared_with_other_sections_say_nothing(self, heading):
        assert not parse_pages(["Reviews\n" + heading + "\n" + ENTRY], "2026-10-19").rows

    @pytest.mark.parametrize("boundary", ["Reviews", "PAID REVIEWS", "Children’s & YA Reviews"])
    def test_the_block_ends_at_the_next_opener_paid_page_or_header(self, boundary):
        # A Nonfiction heading after the boundary belongs to the next block, not this one.
        opener = "Reviews\n" + ENTRY
        rows = parse_pages([opener, boundary + "\nFOOD & COOKING\n" + self.NEXT], "2026-10-19").rows
        assert "9780306406157" not in {row["isbn13"] for row in rows}

    def test_an_unreadable_page_outside_the_section_is_ignored(self):
        from autoscout.pw_issue_parser import UNREADABLE_PAGE

        pages = [UNREADABLE_PAGE, "An advertisement", self.OPENER, self.NEXT, "PAID REVIEWS", UNREADABLE_PAGE]
        parsed = parse_pages(pages, "2026-10-12")
        assert len(parsed.rows) == 2
        assert parsed.unreadable_section_pages == 0

    @pytest.mark.parametrize("position", ["inside", "before_opener", "before_header", "lost_opener", "lost_headerless"])
    def test_an_unreadable_page_inside_or_right_before_the_section_is_counted(self, position):
        from autoscout.pw_issue_parser import UNREADABLE_PAGE

        pages = {
            "inside": [self.OPENER, UNREADABLE_PAGE, self.NEXT],
            "before_opener": ["An advertisement", UNREADABLE_PAGE, self.OPENER, self.NEXT],
            "before_header": [UNREADABLE_PAGE, PAGE],
            # The lost page may itself have been the opener of either layout.
            "lost_opener": ["Fiction Reviews", UNREADABLE_PAGE, ENTRY, PAGE],
            "lost_headerless": ["An advertisement", UNREADABLE_PAGE, "FOOD & COOKING\n" + ENTRY, self.NEXT],
        }[position]
        assert parse_pages(pages, "2026-10-12").unreadable_section_pages == 1

    @pytest.mark.parametrize("title", ["Fiction", "Poetry", "Nonfiction"])
    def test_a_book_title_is_not_section_evidence(self, title):
        second = self.NEXT.replace("A Later Invention", title)
        parsed = parse_pages([self.OPENER, second], "2026-10-19")
        assert [row["title"] for row in parsed.rows] == ["An Invented Coast", title]


def test_title_repair_drops_page_header_and_kerning_gaps():
    from autoscout.pw_issue_parser import _repair_title_glyphs

    assert (
        _repair_title_glyphs("50 PUBLISHERS WEEKL Y A UGUST 31 & SEPTEMBER 7, 2026 How to Exi(s)t: A Poet’ s Memoir")
        == "How to Exi(s)t: A Poet’s Memoir"
    )
    assert (
        _repair_title_glyphs("How W e Quit , and Why It (& Didn ’t) Matter")
        == "How We Quit, and Why It (& Didn’t) Matter"
    )
    assert (
        _repair_title_glyphs("Even the Good Girls Will Cry: A ’90s Rock Memoir")
        == "Even the Good Girls Will Cry: A ’90s Rock Memoir"
    )


def test_title_repair_keeps_a_real_standalone_capital():
    from autoscout.pw_issue_parser import _repair_title_glyphs

    assert _repair_title_glyphs("Henry V and the Battle of Agincourt") == "Henry V and the Battle of Agincourt"
    assert _repair_title_glyphs("V for Victory") == "V for Victory"
    assert _repair_title_glyphs("T ough Guy: A Memoir") == "Tough Guy: A Memoir"


@pytest.mark.parametrize(
    ("raw", "repaired"),
    [
        ("Henry V at Agincourt", "Henry V at Agincourt"),
        ("Generation Y in the Workplace", "Generation Y in the Workplace"),
        ("The Y chromosome", "The Y chromosome"),
        ("Y ou and V oices", "You and Voices"),
        ("T in Can Coast and W as It W orth It", "Tin Can Coast and Was It Worth It"),
        ("The Tari/f_f Wars", "The Tariff Wars"),
        ("Di/f_f_i cult Women and Ine/f_f_i ciency", "Difficult Women and Inefficiency"),
        ("The /T_h ree /f_i res", "The Three fires"),
    ],
)
def test_title_repair_tells_glyph_gaps_from_real_spaces(raw, repaired):
    from autoscout.pw_issue_parser import _repair_title_glyphs

    assert _repair_title_glyphs(raw) == repaired


@pytest.mark.parametrize(
    ("raw", "repaired"),
    [
        ("T o See Beyond: Essays", "To See Beyond: Essays"),
        ("How W e Live", "How We Live"),
        ("O/f_f the Record", "Off the Record"),
        ("Sta/f_f of Life", "Staff of Life"),
        ("The /f_l at Earth", "The flat Earth"),
    ],
)
def test_title_repair_joins_single_letter_fragments_and_keeps_function_words(raw, repaired):
    from autoscout.pw_issue_parser import _repair_title_glyphs

    assert _repair_title_glyphs(raw) == repaired


def test_title_repair_closes_gaps_before_a_bracket_and_a_period():
    from autoscout.pw_issue_parser import _repair_title_glyphs

    assert _repair_title_glyphs("Us v . Them: A Love Story (Sort of )") == "Us v. Them: A Love Story (Sort of)"
    assert _repair_title_glyphs("Ferris Bueller... You’re My Hero") == "Ferris Bueller... You’re My Hero"


@pytest.mark.parametrize(
    ("raw", "repaired"),
    [
        (
            "The Age of Confi scation: Making and T aking Property",
            "The Age of Confiscation: Making and Taking Property",
        ),
        ("How to Be Less Useful: The Joy of Ineffi ciency", "How to Be Less Useful: The Joy of Inefficiency"),
        ("In Defense of Aff ordability, Dignity, and Democracy", "In Defense of Affordability, Dignity, and Democracy"),
        ("Confl ict and the fi rst Refl ection", "Conflict and the first Reflection"),
        ("The fl at Earth", "The flat Earth"),
    ],
)
def test_title_repair_joins_the_gap_after_a_plain_letter_ligature(raw, repaired):
    from autoscout.pw_issue_parser import _repair_title_glyphs

    assert _repair_title_glyphs(raw) == repaired


@pytest.mark.parametrize(
    "title",
    [
        "Off the Record",
        "Staff of Life",
        "The Sheriff at High Noon",
        "A Cliff in Winter",
        "Stuff that Matters",
        "Jeff goes to Washington",
        "Sci-fi movies and Wi-Fi nights",
        "Sufi mystics",
        "The Playoff Years",
    ],
)
def test_title_repair_keeps_real_spaces_after_words_ending_in_a_ligature(title):
    from autoscout.pw_issue_parser import _repair_title_glyphs

    assert _repair_title_glyphs(title) == title
