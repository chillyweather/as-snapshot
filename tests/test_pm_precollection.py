"""The PM deals pass, run against a fake mailbox (Issue #943)."""

from __future__ import annotations

import imaplib
import json
from dataclasses import dataclass
from dataclasses import field
from datetime import UTC
from datetime import date
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from autoscout.pm_mailbox import REASON_CREDENTIALS
from autoscout.pm_mailbox import REASON_LOGIN
from autoscout.pm_mailbox import REASON_PROTOCOL
from autoscout.pm_mailbox import REASON_TIMEOUT
from autoscout.pm_mailbox import MessageHeader
from autoscout.pm_mailbox import PmMailboxError
from autoscout.pm_mailbox import PmSource
from autoscout.pm_precollection import STOP_MAX_MESSAGES
from autoscout.pm_precollection import collect_pm_deals_command
from autoscout.pm_precollection import run_pass
from tests.pm_deal_fixtures import LUNCH_SUBJECT
from tests.pm_deal_fixtures import PM_SENDER
from tests.pm_deal_fixtures import deal_text
from tests.pm_deal_fixtures import message

NOW = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)
PASSWORD = "synthetic-app-password"
CREDENTIALS = {"AUTOSCOUT_PM_IMAP_USER": "pm-mailbox@example.com", "AUTOSCOUT_PM_IMAP_PASSWORD": PASSWORD}


@dataclass
class FakeMailbox:
    """A folder of messages: uid -> (sender, subject, sent, raw)."""

    messages: dict[int, tuple[str, str, datetime, bytes]] = field(default_factory=dict)
    uidvalidity: int = 7
    fetched: list[int] = field(default_factory=list)
    closed: int = 0
    fail_fetch: BaseException | None = None

    def add(self, uid: int, raw: bytes, *, subject: str, sender: str = PM_SENDER, sent: datetime | None = None) -> None:
        self.messages[uid] = (sender, subject, sent or datetime(2026, 11, 3, 14, 33, tzinfo=UTC), raw)

    def headers_since(self, since: date, senders: tuple[str, ...]) -> list[MessageHeader]:
        # The server's FROM search matches substrings, so it may return more than the declared senders.
        return [
            MessageHeader(uid=uid, sender=sender, subject=subject, date=sent)
            for uid, (sender, subject, sent, _) in sorted(self.messages.items())
        ]

    def fetch(self, uid: int) -> bytes:
        if self.fail_fetch is not None:
            raise self.fail_fetch
        self.fetched.append(uid)
        return self.messages[uid][3]

    def close(self) -> None:
        self.closed += 1


def _config(tmp_path: Path, **overrides: Any) -> Path:
    declared = {
        "name": "Publishers Marketplace Deals",
        "contract_family": "pm-deal-email",
        "mailbox": {
            "host": "imap.example.com",
            "port": 993,
            "folder": "PM",
            "user_env": "AUTOSCOUT_PM_IMAP_USER",
            "password_env": "AUTOSCOUT_PM_IMAP_PASSWORD",
        },
        "senders": [PM_SENDER],
        "subjects": {"daily_deals": "Today's Deals from PublishersMarketplace.com"},
        "max_message_age_days": 14,
        "max_messages_per_pass": 30,
        "collection_file": "data/pm/collection.json",
        "attempt_file": "data/pm/attempt.json",
        "watermark_file": "data/pm/watermark.json",
        **overrides,
    }
    path = tmp_path / "pm-deal-email.json"
    path.write_text(json.dumps(declared), encoding="utf-8")
    return path


def _read(tmp_path: Path, name: str) -> dict[str, Any]:
    return json.loads((tmp_path / "data" / "pm" / name).read_text(encoding="utf-8"))


def _run(tmp_path: Path, mailbox: FakeMailbox, *, now: datetime = NOW, **config: Any) -> dict[str, Any]:
    opened: list[tuple[str, str]] = []

    def opener(source: PmSource, user: str, password: str) -> FakeMailbox:
        opened.append((user, password))
        return mailbox

    summary = run_pass(
        _config(tmp_path, **config),
        now=now,
        opener=opener,
        environ=CREDENTIALS,
        key_file=tmp_path / "missing.env",
    )
    assert opened == [("pm-mailbox@example.com", PASSWORD)]
    return summary


def test_an_empty_folder_writes_an_empty_result(tmp_path: Path) -> None:
    summary = _run(tmp_path, FakeMailbox())

    assert summary["status"] == "collected"
    assert summary["messages_read"] == 0
    assert _read(tmp_path, "collection.json")["rows"] == []
    assert _read(tmp_path, "attempt.json")["status"] == "collected"


def test_a_deals_email_becomes_rows_and_the_summary_has_counts_only(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(), subject="Today's Deals from PublishersMarketplace.com")

    summary = _run(tmp_path, mailbox)

    assert summary == {
        "status": "collected",
        "collected_at": NOW.isoformat(),
        "messages_read": 1,
        "messages_skipped_by_rule": 0,
        "messages_unreadable": {},
        "deals_parsed": 5,
        "deals_new": 5,
        "deals_rejected": {"publisher_not_found": 1, "title_not_found": 1, "untitled_deal": 1},
        "deals_in_result": 5,
        "stop_reason": "",
    }
    result = _read(tmp_path, "collection.json")
    assert result["schema_version"] == 2  # Version 2 adds the rights-lead fields (#948).
    row = next(row for row in result["rows"] if row["title"] == "THE SALT ROADS: HOW TRADE MADE THE SEA")
    assert row["message_id"] == "<synthetic-1@example.com>"
    assert row["newsletter_kind"] == "daily_deals"
    assert row["category"] == "History"
    assert row["logline"]  # The logline is kept in the Git-ignored result only.
    assert result["messages"][0]["deals"] == 5
    assert {rejection["reason_code"] for rejection in result["messages"][0]["rejections"]} == {
        "publisher_not_found",
        "title_not_found",
        "untitled_deal",
    }
    assert mailbox.closed == 1


def test_other_senders_and_other_newsletters_are_never_fetched(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(subject=LUNCH_SUBJECT), subject=LUNCH_SUBJECT)
    mailbox.add(2, message(), subject="Receipt", sender="sales@publishersmarketplace.com")
    mailbox.add(3, message(), subject="Today's Deals from PublishersMarketplace.com", sender="someone@example.com")
    mailbox.add(4, message(), subject="Today's Deals from PublishersMarketplace.com")

    summary = _run(tmp_path, mailbox)

    assert mailbox.fetched == [4]
    assert summary["messages_read"] == 1
    assert summary["messages_skipped_by_rule"] == 3
    assert _read(tmp_path, "watermark.json") == {"uidvalidity": 7, "last_uid": 4}


def test_the_watermark_advances_and_a_second_pass_reads_nothing(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(), subject="Today's Deals from PublishersMarketplace.com")
    _run(tmp_path, mailbox)
    mailbox.fetched.clear()

    second = _run(tmp_path, mailbox)

    assert mailbox.fetched == []
    assert second["messages_read"] == 0
    assert second["deals_in_result"] == 5


def test_a_new_email_adds_only_its_new_deals(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(
        1,
        message(deal_text("THE FIRST BOOK"), message_id="<a@x>"),
        subject="Today's Deals from PublishersMarketplace.com",
    )
    _run(tmp_path, mailbox)
    mailbox.add(
        2,
        message(deal_text("THE FIRST BOOK", footer=False) + deal_text("THE SECOND BOOK"), message_id="<b@x>"),
        subject="Today's Deals from PublishersMarketplace.com",
    )

    summary = _run(tmp_path, mailbox)

    assert mailbox.fetched == [1, 2]
    assert (summary["deals_parsed"], summary["deals_new"], summary["deals_in_result"]) == (2, 1, 2)
    first = next(row for row in _read(tmp_path, "collection.json")["rows"] if row["title"] == "THE FIRST BOOK")
    assert first["message_id"] == "<a@x>"


def test_a_uidvalidity_change_reads_again_without_duplicating_deals(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(), subject="Today's Deals from PublishersMarketplace.com")
    _run(tmp_path, mailbox)
    mailbox.uidvalidity = 8
    mailbox.fetched.clear()

    summary = _run(tmp_path, mailbox)

    assert mailbox.fetched == [1]
    assert (summary["deals_new"], summary["deals_in_result"]) == (0, 5)
    assert _read(tmp_path, "watermark.json") == {"uidvalidity": 8, "last_uid": 1}


def test_messages_older_than_the_maximum_age_are_not_read_and_old_rows_drop(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    old = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
    mailbox.add(
        1,
        message(deal_text("AN OLD BOOK"), sent=old, message_id="<old@x>"),
        subject="Today's Deals from PublishersMarketplace.com",
        sent=old,
    )
    mailbox.add(
        2,
        message(deal_text("A NEW BOOK"), message_id="<new@x>"),
        subject="Today's Deals from PublishersMarketplace.com",
    )

    summary = _run(tmp_path, mailbox)

    assert mailbox.fetched == [2]
    assert summary["deals_in_result"] == 1
    # A row whose email has aged past the maximum leaves the result on a later pass.
    later = _run(tmp_path, mailbox, now=datetime(2026, 12, 1, tzinfo=UTC))
    assert later["deals_in_result"] == 0


def test_the_maximum_message_count_cuts_a_pass_short_and_the_next_continues(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    for uid in (1, 2, 3):
        mailbox.add(
            uid,
            message(deal_text(f"BOOK NUMBER {uid}"), message_id=f"<{uid}@x>"),
            subject="Today's Deals from PublishersMarketplace.com",
        )

    first = _run(tmp_path, mailbox, max_messages_per_pass=2)

    assert (first["status"], first["stop_reason"], first["messages_read"]) == ("partial", STOP_MAX_MESSAGES, 2)
    assert _read(tmp_path, "watermark.json")["last_uid"] == 2
    assert _read(tmp_path, "attempt.json")["status"] == "partial"
    second = _run(tmp_path, mailbox, max_messages_per_pass=2)
    assert (second["status"], second["messages_read"], second["deals_in_result"]) == ("collected", 1, 3)


def test_a_login_failure_is_named_and_writes_no_result(tmp_path: Path) -> None:
    def refuse(source: PmSource, user: str, password: str) -> FakeMailbox:
        raise PmMailboxError(REASON_LOGIN, "the PM mailbox refused the login")

    with pytest.raises(PmMailboxError) as caught:
        run_pass(_config(tmp_path), now=NOW, opener=refuse, environ=CREDENTIALS, key_file=tmp_path / "none")

    assert caught.value.reason_code == REASON_LOGIN
    assert _read(tmp_path, "attempt.json")["reason_code"] == REASON_LOGIN
    assert not (tmp_path / "data" / "pm" / "collection.json").exists()


def test_a_timeout_is_named_and_leaves_the_watermark_alone(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(), subject="Today's Deals from PublishersMarketplace.com")
    _run(tmp_path, mailbox)
    mailbox.add(2, message(message_id="<2@x>"), subject="Today's Deals from PublishersMarketplace.com")
    mailbox.fail_fetch = TimeoutError()

    with pytest.raises(PmMailboxError) as caught:
        _run(tmp_path, mailbox)

    assert caught.value.reason_code == REASON_TIMEOUT
    assert _read(tmp_path, "attempt.json")["status"] == "not_collected"
    assert _read(tmp_path, "watermark.json")["last_uid"] == 1
    assert mailbox.closed == 2


def test_credentials_come_from_the_key_file_when_the_environment_has_none(tmp_path: Path) -> None:
    key_file = tmp_path / "pm-mailbox.env"
    key_file.write_text(
        f"AUTOSCOUT_PM_IMAP_USER=pm-mailbox@example.com\nAUTOSCOUT_PM_IMAP_PASSWORD={PASSWORD}\n", encoding="utf-8"
    )
    seen: list[tuple[str, str]] = []

    def opener(source: PmSource, user: str, password: str) -> FakeMailbox:
        seen.append((user, password))
        return FakeMailbox()

    run_pass(_config(tmp_path), now=NOW, opener=opener, environ={}, key_file=key_file)

    assert seen == [("pm-mailbox@example.com", PASSWORD)]


def test_missing_credentials_are_a_named_failure(tmp_path: Path) -> None:
    with pytest.raises(PmMailboxError) as caught:
        run_pass(_config(tmp_path), now=NOW, environ={}, key_file=tmp_path / "missing.env")

    assert caught.value.reason_code == REASON_CREDENTIALS
    assert _read(tmp_path, "attempt.json")["reason_code"] == REASON_CREDENTIALS


def test_the_password_is_never_written_or_printed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(), subject="Today's Deals from PublishersMarketplace.com")
    summary = _run(tmp_path, mailbox)

    written = "".join(path.read_text(encoding="utf-8") for path in (tmp_path / "data" / "pm").iterdir())
    assert PASSWORD not in written
    assert PASSWORD not in json.dumps(summary)
    assert "pm-mailbox@example.com" not in written


def test_the_command_prints_a_named_failure_and_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AUTOSCOUT_PM_IMAP_USER", raising=False)
    monkeypatch.delenv("AUTOSCOUT_PM_IMAP_PASSWORD", raising=False)
    monkeypatch.setattr("autoscout.pm_mailbox.PM_MAILBOX_KEY_FILE", tmp_path / "missing.env")

    assert collect_pm_deals_command(_config(tmp_path)) == 2
    assert json.loads(capsys.readouterr().out)["reason_code"] == REASON_CREDENTIALS


def test_the_committed_declaration_loads_and_names_no_mailbox_owner() -> None:
    from autoscout.pm_mailbox import load_source

    path = Path(__file__).resolve().parents[1] / "configs" / "pm-deal-email.json"
    source = load_source(path)

    assert source.folder == "PM"
    assert source.senders == ("daily@publisherslunch.com",)
    assert "@gmail.com" not in path.read_text(encoding="utf-8")
    assert source.collection_file.parent.name == "pm-precollection"


def test_a_lost_result_is_rebuilt_even_though_its_watermark_survives(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(), subject="Today's Deals from PublishersMarketplace.com")
    _run(tmp_path, mailbox)
    (tmp_path / "data" / "pm" / "collection.json").unlink()
    mailbox.fetched.clear()

    summary = _run(tmp_path, mailbox)

    assert mailbox.fetched == [1]
    assert (summary["deals_new"], summary["deals_in_result"]) == (5, 5)


def test_a_deal_that_reappears_after_its_first_email_expired_is_kept(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    first = datetime(2026, 10, 25, 9, 0, tzinfo=UTC)
    mailbox.add(
        1,
        message(deal_text("THE RETURNING BOOK"), sent=first, message_id="<first@x>"),
        subject="Today's Deals from PublishersMarketplace.com",
        sent=first,
    )
    _run(tmp_path, mailbox)
    later = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
    mailbox.add(
        2,
        message(deal_text("THE RETURNING BOOK"), sent=later, message_id="<again@x>"),
        subject="Today's Deals from PublishersMarketplace.com",
        sent=later,
    )

    summary = _run(tmp_path, mailbox, now=datetime(2026, 11, 21, tzinfo=UTC))

    rows = _read(tmp_path, "collection.json")["rows"]
    assert [row["message_id"] for row in rows] == ["<again@x>"]
    assert summary["deals_new"] == 1


def test_a_connection_dropped_by_the_server_is_a_named_failure(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(), subject="Today's Deals from PublishersMarketplace.com")
    mailbox.fail_fetch = imaplib.IMAP4.abort("socket error: EOF")

    with pytest.raises(PmMailboxError) as caught:
        _run(tmp_path, mailbox)

    assert caught.value.reason_code == REASON_PROTOCOL
    assert _read(tmp_path, "attempt.json") | {"attempted_at": ""} == {
        "attempted_at": "",
        "status": "not_collected",
        "reason_code": REASON_PROTOCOL,
        "counts": {},
    }


def test_a_repeated_deal_expires_by_its_newest_email(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    for uid, day in ((1, 1), (2, 10)):
        sent = datetime(2026, 11, day, 9, 0, tzinfo=UTC)
        mailbox.add(
            uid,
            message(deal_text("THE REPEATED BOOK"), sent=sent, message_id=f"<{day}@x>"),
            subject="Today's Deals from PublishersMarketplace.com",
            sent=sent,
        )
    _run(tmp_path, mailbox, now=datetime(2026, 11, 11, tzinfo=UTC))

    summary = _run(tmp_path, mailbox, now=datetime(2026, 11, 16, tzinfo=UTC))

    rows = _read(tmp_path, "collection.json")["rows"]
    assert summary["deals_in_result"] == 1
    assert rows[0]["message_id"] == "<1@x>"  # The first email stays the deal's record.
    assert rows[0]["last_seen_message_date"].startswith("2026-11-10")


def test_a_watermark_in_its_own_directory_is_written(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    mailbox.add(1, message(), subject="Today's Deals from PublishersMarketplace.com")

    _run(tmp_path, mailbox, watermark_file="state/elsewhere/watermark.json")

    saved = json.loads((tmp_path / "state" / "elsewhere" / "watermark.json").read_text(encoding="utf-8"))
    assert saved == {"uidvalidity": 7, "last_uid": 1}


def test_an_undecodable_email_is_counted_and_the_pass_goes_on(tmp_path: Path) -> None:
    mailbox = FakeMailbox()
    broken = message().replace(b'charset="utf-8"', b'charset="x-no-such-charset"')
    assert broken != message()
    mailbox.add(1, broken, subject="Today's Deals from PublishersMarketplace.com")
    mailbox.add(2, message(message_id="<good@x>"), subject="Today's Deals from PublishersMarketplace.com")

    summary = _run(tmp_path, mailbox)

    assert summary["messages_unreadable"] == {"unreadable_plain_text": 1}
    assert summary["deals_parsed"] == 5
    assert _read(tmp_path, "attempt.json")["status"] == "collected"
    assert _read(tmp_path, "watermark.json")["last_uid"] == 2
