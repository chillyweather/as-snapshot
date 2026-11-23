"""The PM mailbox adapter speaks only read-only IMAP (Issue #943).

This is the one test of mechanics rather than outcomes, kept because
"read-only" is a promise to the operator: a fake IMAP server records every
command the real adapter sends, and refuses everything but the read-only set.
"""

from __future__ import annotations

import imaplib
import json
import re
import socket
import threading
from collections.abc import Iterator
from datetime import UTC
from datetime import datetime
from pathlib import Path

import pytest

from autoscout.pm_mailbox import REASON_FOLDER
from autoscout.pm_mailbox import REASON_LOGIN
from autoscout.pm_mailbox import REASON_PROTOCOL
from autoscout.pm_mailbox import PmMailboxError
from autoscout.pm_mailbox import PmSource
from autoscout.pm_mailbox import open_imap_mailbox
from autoscout.pm_precollection import run_pass
from tests.pm_deal_fixtures import LUNCH_SUBJECT
from tests.pm_deal_fixtures import message

READ_ONLY_COMMANDS = {"CAPABILITY", "LOGIN", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}
DEALS_SUBJECT = "Today's Deals from PublishersMarketplace.com"


class FakeImapServer:
    """A one-connection IMAP server holding one folder, PM, with two messages."""

    def __init__(
        self, *, refuse_login: bool = False, folder: str = "PM", drop_on_body: bool = False, uid_last: bool = False
    ) -> None:
        self.commands: list[str] = []
        self.uid_last = uid_last
        self.drop_on_body = drop_on_body
        self.refuse_login = refuse_login
        self.folder = folder
        self.messages = {
            1: message(subject=LUNCH_SUBJECT, message_id="<lunch@x>"),
            2: message(subject=DEALS_SUBJECT, message_id="<deals@x>"),
        }
        self._listener = socket.socket()
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(1)
        self.port = self._listener.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        connection, _ = self._listener.accept()
        with connection, connection.makefile("rwb") as stream:
            stream.write(b"* OK fake IMAP ready\r\n")
            stream.flush()
            while True:
                line = stream.readline()
                if not line:
                    return
                tag, _, rest = line.decode("utf-8").rstrip("\r\n").partition(" ")
                words = rest.split(" ")
                name = " ".join(words[:2]).upper() if words[0].upper() == "UID" else words[0].upper()
                self.commands.append(rest if name == "UID FETCH" else name)
                if not self._answer(stream, tag, name, rest):
                    return

    def _answer(self, stream, tag: str, name: str, rest: str) -> bool:  # type: ignore[no-untyped-def]
        def send(*lines: bytes) -> None:
            stream.write(b"".join(lines))
            stream.flush()

        ok = f"{tag} OK done\r\n".encode()
        if name == "CAPABILITY":
            send(b"* CAPABILITY IMAP4rev1\r\n", ok)
        elif name == "LOGIN":
            send(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials\r\n".encode() if self.refuse_login else ok)
        elif name == "EXAMINE":
            if f'"{self.folder}"' not in rest:
                send(f"{tag} NO no such folder\r\n".encode())
            else:
                send(
                    b"* 2 EXISTS\r\n", b"* OK [UIDVALIDITY 7] UIDs valid\r\n", f"{tag} OK [READ-ONLY] done\r\n".encode()
                )
        elif name == "UID SEARCH":
            send(b"* SEARCH 1 2\r\n", ok)
        elif name == "UID FETCH":
            uids = [int(value) for value in re.search(r"UID FETCH ([\d,]+)", rest).group(1).split(",")]  # type: ignore[union-attr]
            headers_only = "HEADER.FIELDS" in rest
            if self.drop_on_body and not headers_only:
                return False  # The server drops the connection mid-session.
            for uid in uids:
                raw = self.messages[uid]
                body = raw.split(b"\n\n", 1)[0] + b"\r\n\r\n" if headers_only else raw
                section = "BODY[HEADER.FIELDS (FROM SUBJECT DATE)]" if headers_only else "BODY[]"
                if self.uid_last:
                    # IMAP allows the UID after the literal.
                    send(f"* {uid} FETCH ({section} {{{len(body)}}}\r\n".encode(), body, f" UID {uid})\r\n".encode())
                else:
                    send(f"* {uid} FETCH (UID {uid} {section} {{{len(body)}}}\r\n".encode(), body, b")\r\n")
            send(ok)
        elif name == "LOGOUT":
            send(b"* BYE logging out\r\n", ok)
            return False
        else:
            send(f"{tag} BAD not a read-only command\r\n".encode())
        return True

    def client(self, host: str, port: int, timeout: float) -> imaplib.IMAP4:
        return imaplib.IMAP4("127.0.0.1", self.port, timeout=5)


@pytest.fixture
def server() -> Iterator[FakeImapServer]:
    fake = FakeImapServer()
    yield fake


def _config(tmp_path: Path) -> Path:
    path = tmp_path / "pm.json"
    path.write_text(
        json.dumps(
            {
                "name": "Publishers Marketplace Deals",
                "contract_family": "pm-deal-email",
                "mailbox": {
                    "host": "imap.example.com",
                    "port": 993,
                    "folder": "PM",
                    "user_env": "PM_USER",
                    "password_env": "PM_PASSWORD",
                },
                "senders": ["daily@publisherslunch.com"],
                "subjects": {"daily_deals": DEALS_SUBJECT},
                "max_message_age_days": 14,
                "max_messages_per_pass": 30,
                "collection_file": "out/collection.json",
                "attempt_file": "out/attempt.json",
                "watermark_file": "out/watermark.json",
            }
        ),
        encoding="utf-8",
    )
    return path


def _source(folder: str = "PM") -> PmSource:
    return PmSource(
        name="PM",
        host="imap.example.com",
        port=993,
        folder=folder,
        user_env="PM_USER",
        password_env="PM_PASSWORD",
        senders=("daily@publisherslunch.com",),
        subjects={"daily_deals": DEALS_SUBJECT},
        max_message_age_days=14,
        max_messages_per_pass=30,
        collection_file=Path("unused"),
        attempt_file=Path("unused"),
        watermark_file=Path("unused"),
    )


def test_a_whole_pass_sends_only_read_only_commands(tmp_path: Path, server: FakeImapServer) -> None:
    def opener(source: PmSource, user: str, password: str):  # type: ignore[no-untyped-def]
        return open_imap_mailbox(source, user, password, client_factory=server.client)

    summary = run_pass(
        _config(tmp_path),
        now=datetime(2026, 11, 4, tzinfo=UTC),
        opener=opener,
        environ={"PM_USER": "pm-mailbox@example.com", "PM_PASSWORD": "secret"},
        key_file=tmp_path / "none",
    )

    names = {command if not command.startswith("UID FETCH") else "UID FETCH" for command in server.commands}
    assert names <= READ_ONLY_COMMANDS
    assert "EXAMINE" in names
    fetches = [command for command in server.commands if command.startswith("UID FETCH")]
    assert fetches and all("BODY.PEEK[" in command for command in fetches)
    # Only the deals email is fetched in full; the Lunch Deluxe email is skipped from its headers.
    assert [command for command in fetches if "BODY.PEEK[]" in command] == ["UID FETCH 2 (BODY.PEEK[])"]
    assert (summary["messages_read"], summary["messages_skipped_by_rule"], summary["deals_parsed"]) == (1, 1, 5)
    assert server.commands[-1] == "LOGOUT"


def test_headers_with_the_uid_after_the_literal_are_read() -> None:
    fake = FakeImapServer(uid_last=True)
    mailbox = open_imap_mailbox(_source(), "pm-mailbox@example.com", "secret", client_factory=fake.client)

    headers = mailbox.headers_since(datetime(2026, 11, 1, tzinfo=UTC).date(), ("daily@publisherslunch.com",))
    mailbox.close()

    assert [(header.uid, header.subject) for header in headers] == [(1, LUNCH_SUBJECT), (2, DEALS_SUBJECT)]


def test_a_refused_login_is_a_named_failure() -> None:
    refusing = FakeImapServer(refuse_login=True)

    with pytest.raises(PmMailboxError) as caught:
        open_imap_mailbox(_source(), "pm-mailbox@example.com", "wrong", client_factory=refusing.client)

    assert caught.value.reason_code == REASON_LOGIN
    assert "wrong" not in str(caught.value)


def test_a_missing_folder_is_a_named_failure() -> None:
    fake = FakeImapServer(folder="Other")

    with pytest.raises(PmMailboxError) as caught:
        open_imap_mailbox(_source(), "pm-mailbox@example.com", "secret", client_factory=fake.client)

    assert caught.value.reason_code == REASON_FOLDER
    assert "LOGOUT" in fake.commands


def test_a_connection_dropped_during_a_fetch_is_a_named_failure() -> None:
    dropping = FakeImapServer(drop_on_body=True)
    mailbox = open_imap_mailbox(_source(), "pm-mailbox@example.com", "secret", client_factory=dropping.client)

    with pytest.raises(PmMailboxError) as caught:
        mailbox.fetch(2)

    assert caught.value.reason_code == REASON_PROTOCOL
    mailbox.close()


def test_the_default_connection_verifies_the_server_certificate(monkeypatch: pytest.MonkeyPatch) -> None:
    import ssl

    seen: dict[str, object] = {}

    def capture(host: str, port: int, *, ssl_context: ssl.SSLContext, timeout: float) -> imaplib.IMAP4:
        seen.update(host=host, context=ssl_context)
        raise OSError("not connecting in a test")

    monkeypatch.setattr(imaplib, "IMAP4_SSL", capture)

    with pytest.raises(PmMailboxError):
        open_imap_mailbox(_source(), "pm-mailbox@example.com", "secret")

    context = seen["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
