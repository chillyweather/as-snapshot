"""Synthetic Publishers Marketplace deals emails for the PM tests (Issue #943).

The messages follow the structure of the real "Today's Deals" email: bold
section, country and category headings, one paragraph per deal with
dealmaker profile links, contact lines after a paragraph, and the account
footer. Every name, title and logline is invented; no PM text is copied.
"""

from __future__ import annotations

from datetime import datetime
from email.message import EmailMessage
from email.utils import format_datetime


def profile(number: int) -> str:
    return f"https://www.publishersmarketplace.com/dealmakers/detail.cgi?id={number}"


DAILY_DEALS_TEXT = f"""Here are the most recent deals as reported to Publishers Marketplace.

**Tuesday, November 3, 2026**

Today's Deals

**Reminders** A synthetic reminder paragraph that is not a deal.

All Deals https://www.publishersmarketplace.com/deals/

______________________________________________________________________

**Fiction**

**Literary**

Author of PAPER LANTERNS https://bookshop.org/a/1/9780000000001 Ada Quill's THE GLASS ORCHARD, a novel about two sisters who inherit a failing orchard, to Ben Arbor {profile(101)} at Northlight Books {profile(102)}, at auction, by Cora Vale {profile(103)} at Vale Literary {profile(104)} (NA).

**General/Other**

Dana Fenwick's untitled novel about a lighthouse keeper, to Ben Arbor {profile(101)} at Northlight Books {profile(102)}, by Cora Vale {profile(103)} at Vale Literary {profile(104)} (world).

**Non-fiction**

**History**

Senior editor at Harbor Review Iris Mendel's THE SALT ROADS: HOW TRADE MADE THE SEA, a narrative history of the merchants who tied distant ports together, to Jon Pike {profile(201)} and Lena Moss {profile(202)} at Meridian Press {profile(203)}, in a significant deal, for publication in fall 2027, by Omar Reyes {profile(204)} and Tess Lowe {profile(205)} at Park, Lane & Reyes Literary {profile(206)} (world English). Rights also to Kit Hale {profile(207)} at Harbor UK {profile(208)} (UK); and to Vento {profile(209)} (Italy).
omar@example.com mailto:omar@example.com Rights:
rights@example.com mailto:rights@example.com

**Science/Technology**

Dr. Paul Ostrow and Mina Kaur Sethi's THE QUIET MACHINE, arguing that the slowest tools shape the most, to Ruth Fell {profile(301)} at Cobalt {profile(302)}, by Sam Wynn at Wynn Agency (NA).

A paragraph that names a book without its author or a title, to Ruth Fell {profile(301)} at Cobalt {profile(302)} (world).

Zed Orr's BRIGHT FIELDS, a memoir of a farming childhood, by Cora Vale {profile(103)} at Vale Literary {profile(104)} (world).

**Poetry** Gil Marr's NIGHT FERRY, a collection about crossings, to Pia Lund {profile(401)} at Tern Books {profile(402)}, in a nice deal, for publication in spring 2028 (world).

**International rights**

**Latvia**

**Non-fiction**

Hal Brandt's THE LONG WINTER, to Ziedi {profile(501)} (Latvia), by Una Berg {profile(502)} at Baltic Rights {profile(503)}.

**Manage Your Information**           eMail https://www.publishersmarketplace.com/account/editContact.cgi

**This publication is for your individual use only.** A synthetic footer.
"""

DAILY_DEALS_SUBJECT = "Today's Deals from PublishersMarketplace.com"
LUNCH_SUBJECT = "11/3 Publishers Lunch Deluxe: A synthetic headline"
PM_SENDER = "daily@publisherslunch.com"


def message(
    text: str = DAILY_DEALS_TEXT,
    *,
    subject: str = DAILY_DEALS_SUBJECT,
    sender: str = PM_SENDER,
    sent: datetime | None = None,
    message_id: str = "<synthetic-1@example.com>",
    html: bool = True,
) -> bytes:
    """One raw message with a plain-text part and, by default, an HTML alternative."""
    mail = EmailMessage()
    mail["From"] = f"Publishers Lunch <{sender}>"
    mail["To"] = "pm-mailbox@example.com"
    mail["Subject"] = subject
    mail["Message-ID"] = message_id
    mail["Date"] = format_datetime(sent or datetime.fromisoformat("2026-11-03T14:33:03+00:00"))
    mail.set_content(text)
    if html:
        mail.add_alternative("<html><body><p>Synthetic HTML part.</p></body></html>", subtype="html")
    return mail.as_bytes()


def deal_text(title: str, author: str = "Ada Quill", category: str = "History", *, footer: bool = True) -> str:
    """A minimal deals email with one Non-fiction deal, ending at the footer unless *footer* is false."""
    return f"""**Non-fiction**

**{category}**

{author}'s {title}, a synthetic logline, to Ben Arbor {profile(101)} at Northlight Books {profile(102)}, by Cora Vale {profile(103)} at Vale Literary {profile(104)} (world).

""" + ("**Manage Your Information**\n" if footer else "")
