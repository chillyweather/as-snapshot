"""The PM deals mailbox: its declaration, its credentials and a read-only reader (Issue #943).

The reader opens one folder read-only (IMAP ``EXAMINE``) and fetches with
``BODY.PEEK``, so a pass never sets a flag. It issues no ``STORE``, ``COPY``,
``MOVE`` or ``EXPUNGE``. It is a thin adapter behind the :class:`Mailbox`
protocol, so the pass is tested with a fake mailbox.

The account address and its password are not part of the declaration. They
come from the environment, or from the operator's key file outside the
repository, so the configuration never names the mailbox owner.
"""

from __future__ import annotations

import imaplib
import json
import os
import re
import ssl
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from datetime import datetime
from email.utils import parseaddr
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from typing import Protocol

from autoscout.pm_deal_email import message_sender

FAMILY = "pm-deal-email"
# Outside the repository, mode 0600. Read only for a name the environment does not set.
PM_MAILBOX_KEY_FILE = Path.home() / ".config" / "autoscout" / "pm-mailbox.env"
DEFAULT_TIMEOUT_SECONDS = 30.0

REASON_DECLARATION = "pm_declaration"
REASON_CREDENTIALS = "pm_credentials_missing"
REASON_LOGIN = "pm_login_failed"
REASON_CONNECTION = "pm_connection_failed"
REASON_TIMEOUT = "pm_timeout"
REASON_FOLDER = "pm_folder_unavailable"
REASON_PROTOCOL = "pm_imap_error"


class PmMailboxError(Exception):
    """A named failure of the mailbox. Its message never holds a credential."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True)
class PmSource:
    """The declared PM deals source: where its mail is and which messages count."""

    name: str
    host: str
    port: int
    folder: str
    user_env: str
    password_env: str
    senders: tuple[str, ...]
    subjects: Mapping[str, str]
    max_message_age_days: int
    max_messages_per_pass: int
    collection_file: Path
    attempt_file: Path
    # PM resumes from a UID watermark; PW selects by collected issue date and has none (#963).
    watermark_file: Path | None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


def _text(declared: Mapping[str, Any], key: str) -> str:
    value = declared.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PmMailboxError(REASON_DECLARATION, f"the PM source declares no {key}")
    return value.strip()


def _positive_int(declared: Mapping[str, Any], key: str) -> int:
    value = declared.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PmMailboxError(REASON_DECLARATION, f"the PM source declares no positive integer {key}")
    return value


def load_source(config_path: Path, *, family: str = FAMILY) -> PmSource:
    """Read and validate the PM source declaration. Relative paths resolve against the config's folder."""
    try:
        declared = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PmMailboxError(REASON_DECLARATION, f"the PM config cannot be read: {type(exc).__name__}") from None
    if not isinstance(declared, dict):
        raise PmMailboxError(REASON_DECLARATION, "the PM config is not a JSON object")
    if str(declared.get("contract_family", "")).strip() != family:
        raise PmMailboxError(REASON_DECLARATION, f"the PM config declares no contract_family {family}")
    mailbox = declared.get("mailbox")
    if not isinstance(mailbox, dict):
        raise PmMailboxError(REASON_DECLARATION, "the PM source declares no mailbox")
    senders = declared.get("senders")
    if not isinstance(senders, list) or not senders or not all(isinstance(s, str) and "@" in s for s in senders):
        raise PmMailboxError(REASON_DECLARATION, "the PM source declares no sender addresses")
    subjects = declared.get("subjects")
    if (
        not isinstance(subjects, dict)
        or not subjects
        or not all(isinstance(k, str) and isinstance(v, str) and v.strip() for k, v in subjects.items())
    ):
        raise PmMailboxError(REASON_DECLARATION, "the PM source declares no subject per newsletter kind")
    base = config_path.parent
    timeout = mailbox.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)
    if not isinstance(timeout, int | float) or isinstance(timeout, bool) or timeout <= 0:
        raise PmMailboxError(REASON_DECLARATION, "the PM mailbox declares no positive timeout_seconds")
    return PmSource(
        name=_text(declared, "name"),
        host=_text(mailbox, "host"),
        port=_positive_int(mailbox, "port"),
        folder=_text(mailbox, "folder"),
        user_env=_text(mailbox, "user_env"),
        password_env=_text(mailbox, "password_env"),
        senders=tuple(sender.strip().casefold() for sender in senders),
        subjects={kind: subject.strip() for kind, subject in subjects.items()},
        max_message_age_days=_positive_int(declared, "max_message_age_days"),
        max_messages_per_pass=_positive_int(declared, "max_messages_per_pass"),
        collection_file=(base / _text(declared, "collection_file")).resolve(),
        attempt_file=(base / _text(declared, "attempt_file")).resolve(),
        watermark_file=(base / _text(declared, "watermark_file")).resolve() if family == FAMILY else None,
        timeout_seconds=float(timeout),
    )


def load_credential(name: str, environ: Mapping[str, str] | None = None, key_file: Path | None = None) -> str | None:
    """Return the value of *name* from the environment, or else from the operator's key file."""
    env = os.environ if environ is None else environ
    value = str(env.get(name, "")).strip()
    if value:
        return value
    path = PM_MAILBOX_KEY_FILE if key_file is None else key_file
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, separator, found = line.partition("=")
        if separator and key.strip() == name and found.strip():
            return found.strip()
    return None


@dataclass(frozen=True)
class MessageHeader:
    uid: int
    sender: str
    subject: str
    date: datetime | None
    # The display name of From, e.g. "Publishers Weekly"; the address alone cannot tell
    # PW's issue email from another newsletter sent from the same address.
    sender_name: str = ""


class Mailbox(Protocol):
    """One folder of the mailbox, opened read-only."""

    @property
    def uidvalidity(self) -> int: ...

    def headers_since(self, since: date, senders: tuple[str, ...]) -> list[MessageHeader]:
        """Headers of the messages from *senders* that arrived on or after *since*."""
        ...

    def fetch(self, uid: int) -> bytes:
        """The raw bytes of one message, fetched without setting a flag."""
        ...

    def close(self) -> None: ...


MailboxOpener = Callable[[PmSource, str, str], Mailbox]

_IMAP_DATE_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_UIDVALIDITY = re.compile(rb"UIDVALIDITY\s+(\d+)")
_FETCH_UID = re.compile(rb"\bUID\s+(\d+)")


def _imap_date(day: date) -> str:
    return f"{day.day:02d}-{_IMAP_DATE_MONTHS[day.month - 1]}-{day.year}"


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


class ImapMailbox:
    """The read-only IMAP adapter. Only CAPABILITY, LOGIN, EXAMINE, UID SEARCH, UID FETCH and LOGOUT are sent."""

    def __init__(self, client: imaplib.IMAP4, folder: str) -> None:
        self._client = client
        try:
            status, _ = client.select(_quoted(folder), readonly=True)
        except imaplib.IMAP4.error:
            status = "NO"
        if status != "OK":
            self.close()
            raise PmMailboxError(REASON_FOLDER, "the PM folder cannot be opened read-only")
        try:
            _, values = client.response("UIDVALIDITY")
        except imaplib.IMAP4.error:
            values = []
        self._uidvalidity = int(values[0]) if values and values[0] else 0

    @property
    def uidvalidity(self) -> int:
        return self._uidvalidity

    def headers_since(self, since: date, senders: tuple[str, ...]) -> list[MessageHeader]:
        try:
            return self._headers_since(since, senders)
        except imaplib.IMAP4.error:
            # imaplib raises IMAP4.abort when the server drops the connection mid-command.
            raise PmMailboxError(REASON_PROTOCOL, "the PM mail server ended the search") from None

    def _headers_since(self, since: date, senders: tuple[str, ...]) -> list[MessageHeader]:
        uids: set[int] = set()
        for sender in senders:
            status, data = self._client.uid("SEARCH", "SINCE", _imap_date(since), "FROM", _quoted(sender))
            if status != "OK":
                raise PmMailboxError(REASON_PROTOCOL, "the PM folder search failed")
            for item in data:
                if isinstance(item, bytes):
                    uids.update(int(value) for value in item.split())
        if not uids:
            return []
        message_set = ",".join(str(uid) for uid in sorted(uids))
        status, data = self._client.uid("FETCH", message_set, "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])")
        if status != "OK":
            raise PmMailboxError(REASON_PROTOCOL, "the PM header fetch failed")
        headers: list[MessageHeader] = []
        for position, item in enumerate(data):
            if not isinstance(item, tuple):
                continue
            uid_match = _FETCH_UID.search(item[0])
            following = data[position + 1] if position + 1 < len(data) else None
            if uid_match is None and isinstance(following, bytes):
                # The server may send the UID after the header literal: "BODY[...] {n} ... UID 4)".
                uid_match = _FETCH_UID.search(following)
            if uid_match is None:
                continue
            headers.append(_header(int(uid_match.group(1)), item[1]))
        return sorted(headers, key=lambda header: header.uid)

    def fetch(self, uid: int) -> bytes:
        try:
            status, data = self._client.uid("FETCH", str(uid), "(BODY.PEEK[])")
        except imaplib.IMAP4.error:
            raise PmMailboxError(REASON_PROTOCOL, "the PM mail server ended the message fetch") from None
        if status != "OK":
            raise PmMailboxError(REASON_PROTOCOL, "the PM message fetch failed")
        for item in data:
            if isinstance(item, tuple) and isinstance(item[1], bytes):
                return item[1]
        raise PmMailboxError(REASON_PROTOCOL, "the PM message fetch returned no body")

    def close(self) -> None:
        try:
            self._client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass


def _header(uid: int, raw: bytes) -> MessageHeader:
    from email import message_from_bytes
    from email import policy

    parsed = message_from_bytes(raw, policy=policy.default)
    try:
        sent = parsedate_to_datetime(str(parsed.get("Date", ""))) if parsed.get("Date") else None
    except (TypeError, ValueError):
        sent = None
    return MessageHeader(
        uid=uid,
        sender=message_sender(str(parsed.get("From", ""))),
        subject=" ".join(str(parsed.get("Subject", "")).split()),
        date=sent,
        sender_name=" ".join(parseaddr(str(parsed.get("From", "")))[0].split()),
    )


def open_imap_mailbox(
    source: PmSource,
    user: str,
    password: str,
    *,
    client_factory: Callable[[str, int, float], imaplib.IMAP4] | None = None,
) -> Mailbox:
    """Connect over SSL, log in and open the declared folder read-only."""

    def ssl_client(host: str, port: int, timeout: float) -> imaplib.IMAP4:
        # Without an explicit context imaplib does not verify the certificate; the password
        # is sent only to a server that proves it is the declared host.
        return imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=timeout)

    factory = client_factory or ssl_client
    try:
        client = factory(source.host, source.port, source.timeout_seconds)
    except TimeoutError:
        raise PmMailboxError(REASON_TIMEOUT, "the PM mail server did not answer in time") from None
    except (OSError, imaplib.IMAP4.error):
        raise PmMailboxError(REASON_CONNECTION, "the PM mail server cannot be reached") from None
    try:
        client.login(user, password)
    except imaplib.IMAP4.error:
        try:
            client.logout()
        except (imaplib.IMAP4.error, OSError):
            pass
        raise PmMailboxError(REASON_LOGIN, "the PM mailbox refused the login") from None
    except TimeoutError:
        raise PmMailboxError(REASON_TIMEOUT, "the PM mail server did not answer in time") from None
    except OSError:
        raise PmMailboxError(REASON_CONNECTION, "the PM mail server closed the connection") from None
    return ImapMailbox(client, source.folder)
