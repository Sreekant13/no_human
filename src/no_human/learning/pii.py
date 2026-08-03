"""Personal-data gate for the human-confirmed learning queue.

A memory that carries a person's home address, phone number, personal email,
payment details, government ID or date of birth is **dropped**, not redacted.
Two reasons, both learned from the incident this module exists to fix:

* a redacted shopping fact is still not a coding rule — nothing of value
  survives the redaction, so keeping the husk only clutters the confirm queue;
* ``"User's shipping address is [REDACTED]"`` is *itself* a privacy signal about
  the user, persisted to the database and shown back to them. Redaction moves
  the disclosure, it does not remove it.

The hard constraint on the other side is that **over-blocking makes the feature
useless**. Engineering prose is full of digits and identifiers, and a gate that
mistakes them for personal data quietly deletes the lessons the product exists
to collect. So every detector here is built to demand *context*, not just shape:

======================  ========================================================
DROPPED                 Why it is safe to drop
======================  ========================================================
street address          Needs an address KEYWORD — one of shipping, home,
                        billing, mailing, delivery or postal, followed by the
                        word "address" — or a dwelling form ("Flat 1", "Apt 4B",
                        "PO Box 12"), or number+name+street-type
                        ("12 Main Street"). Bare "address" never fires, so "IP
                        address", "memory address", "email address" and "MAC
                        address" all survive.
phone number            Needs a phone KEYWORD near a 7+ digit run, or a full
                        E.164 number (``+`` and 10-15 digits). A bare digit run
                        is NEVER a phone number here — that is what would
                        swallow ports, PIDs, versions, timestamps and IDs.
personal email          Only consumer mailbox domains (gmail, yahoo, icloud,
                        proton, …). See KEPT below for the carve-outs.
payment / bank          Needs a payment KEYWORD ("card number", "cvv", "iban",
                        "sort code", "routing number", "bank account") near
                        digits, or a *group-formatted* card number that passes
                        the Luhn check ("4111 1111 1111 1111"). An unformatted
                        16-digit number is not enough on its own — ~10% of long
                        numeric IDs pass Luhn by chance.
government ID           A US SSN in ``NNN-NN-NNNN`` shape, or an explicit
                        keyword ("passport number", "national insurance number",
                        "social security number", "driver's licence number",
                        "teudat zehut", …).
date of birth           "date of birth" / "DOB" / "birthdate" / "born on <date>".
======================  ========================================================

======================  ========================================================
KEPT (never a match)    Why
======================  ========================================================
IP addresses            No detector exists for them at all. ``192.168.1.1``,
                        ``::1`` and ``10.0.0.0/8`` are infrastructure facts.
localhost / ports       ``localhost:3000``, ``127.0.0.1:8080`` — no bare-digit
                        detector can reach them.
test / fake emails      Only consumer domains are flagged, so ``example.com``,
                        ``test.local``, ``*.invalid`` and corporate domains pass.
``noreply`` addresses   ``…@users.noreply.github.com`` and any ``noreply@`` /
                        ``no-reply@`` local part are explicitly allowed.
git author lines        A line beginning ``Author:``/``Committer:``/
                        ``Co-Authored-By:``/``Signed-off-by:``, or containing
                        ``user.email``/``git config``, is exempt from the email
                        detector — the task brief names these as legitimate
                        engineering content.
SHAs, UUIDs, versions   No shape-only numeric detector fires without a keyword.
======================  ========================================================

The deliberate gap, stated plainly rather than left for a reader to discover: a
**corporate** email address (``someone@acme-corp.test``) is NOT dropped. It is
personal data, but exempting it is what keeps git logs, code owners, bug
reporters and support threads minable. If that trade is ever revisited, the
allowlist in :data:`_CONSUMER_EMAIL_DOMAINS` is the single place to change.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["PIIFinding", "find_pii", "contains_pii"]


@dataclass(frozen=True)
class PIIFinding:
    """What kind of personal data was found — never the value itself.

    Only ``kind`` is exposed so a caller can log *why* a memory was dropped
    without writing the personal data into a log file, which would recreate the
    defect one layer down.
    """
    kind: str

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.kind


# --------------------------------------------------------------------------- #
# Street address                                                               #
# --------------------------------------------------------------------------- #
# An address KEYWORD: a qualifier that makes "address" mean a postal address.
# Bare "address" is never enough (IP / MAC / memory / email address).
_ADDRESS_KEYWORD = re.compile(
    r"\b(?:shipping|home|billing|mailing|delivery|postal|residential"
    r"|correspondence)\s+address\b"
    r"|\b(?:zip|post)\s*code\b\s*(?:is|:)?\s*\w"
    r"|\bpostcode\b\s*(?:is|:)?\s*\w",
    re.IGNORECASE,
)
# "<qualifier> address is <something with a digit>". The qualifier decides:
# "IP address is 10.0.0.1" and "MAC address is …" are infrastructure facts and
# must survive, so technical qualifiers are excluded by name rather than the
# whole construction being abandoned.
_QUALIFIED_ADDRESS_IS = re.compile(
    r"\b(\w+)\s+address\s+(?:is\b|:)[^\n]*\d", re.IGNORECASE,
)
_TECHNICAL_ADDRESS_QUALIFIERS = frozenset({
    "ip", "ipv4", "ipv6", "mac", "memory", "email", "e-mail", "bus", "base",
    "virtual", "physical", "network", "host", "server", "contract", "wallet",
    "bind", "listen", "loopback", "broadcast", "multicast", "socket", "peer",
    "return",
})
# A dwelling designator followed by a unit that ENDS the field — "Flat 1,",
# "Apt 4B", "PO Box 12". The terminator requirement is what keeps "flat 1D
# array" and "test suite 3" out.
_DWELLING = re.compile(
    r"\b(?:flat|apt|apartment|p\.?\s?o\.?\s+box|house\s+no)\b\s*\.?\s*"
    r"#?\s*\d+[a-z]?(?=\s*(?:[,;.]|$))",
    re.IGNORECASE | re.MULTILINE,
)
# "12 Main Street", "221B Baker Rd" — number, 1-4 name words, street type.
_STREET_LINE = re.compile(
    r"\b\d{1,5}[A-Za-z]?\s+(?:[A-Z][\w'.-]{1,20}\s+){1,4}"
    # The street TYPE is case-insensitive ("Street"/"street"/"ST"); the name
    # words above are not — requiring real capitalisation is what keeps this off
    # ordinary prose like "3 more test cases".
    r"(?i:street|st|road|rd|avenue|ave|lane|ln|drive|dr|boulevard|blvd|court|ct"
    r"|place|pl|terrace|square|sq|highway|hwy|parkway|pkwy|crescent|close)\b\.?",
)

# --------------------------------------------------------------------------- #
# Phone number                                                                 #
# --------------------------------------------------------------------------- #
_PHONE_KEYWORD = (
    r"(?:phone|telephone|tel|mobile|cell(?:phone)?|whatsapp|fax|sms"
    r"|contact\s+number|call\s+(?:me|him|her|them)\s+(?:at|on))"
)
# Keyword, then within ~40 chars a 7+ digit run (separators allowed).
_PHONE_WITH_KEYWORD = re.compile(
    r"\b" + _PHONE_KEYWORD + r"\b[^\n\d]{0,40}(?:\+?\d[\d\s().-]{5,}\d)",
    re.IGNORECASE,
)
# Full E.164-ish international number: "+" then 10-15 digits with separators.
_E164 = re.compile(r"(?<![\w.+-])\+\d(?:[\s().-]?\d){9,14}(?![\d])")

# --------------------------------------------------------------------------- #
# Personal email                                                               #
# --------------------------------------------------------------------------- #
_EMAIL = re.compile(r"\b([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)\b")
_CONSUMER_EMAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com", "yahoo.com", "yahoo.co.uk", "ymail.com",
    "hotmail.com", "hotmail.co.uk", "outlook.com", "live.com", "msn.com",
    "icloud.com", "me.com", "mac.com", "aol.com", "gmx.com", "gmx.de",
    "protonmail.com", "proton.me", "pm.me", "zoho.com", "yandex.ru",
    "mail.ru", "qq.com", "163.com", "126.com", "naver.com", "walla.co.il",
    "web.de", "free.fr", "orange.fr", "libero.it", "seznam.cz",
})
_EMAIL_EXEMPT_LOCAL = ("noreply", "no-reply", "donotreply", "do-not-reply")
# Lines that are git authorship metadata — explicitly legitimate content.
_GIT_AUTHOR_LINE = re.compile(
    r"^\s*(?:author|committer|co-authored-by|signed-off-by|reported-by"
    r"|reviewed-by|tested-by|acked-by|from)\s*:"
    r"|user\.email|git\s+config|--author",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------- #
# Payment / bank                                                               #
# --------------------------------------------------------------------------- #
_PAYMENT_KEYWORD = re.compile(
    r"\b(?:credit\s+card|debit\s+card|card\s+number|cardholder|card\s+expiry"
    r"|cvv|cvc|security\s+code|expiry\s+date|exp\s+date|iban|swift\s+code|bic"
    r"|sort\s+code|routing\s+number|bank\s+account|account\s+sort)\b"
    r"[^\n]{0,40}\d{4}",
    re.IGNORECASE,
)
# Group-formatted card number: 4-4-4-4 style, separated. Luhn-checked below.
_CARD_GROUPED = re.compile(r"(?<![\d-])(?:\d{4}[ -]){3}\d{3,4}(?![\d-])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?=[A-Z0-9]{11,30}\b)(?:[A-Z0-9]){11,30}\b")


def _luhn_ok(digits: str) -> bool:
    """Luhn checksum — the standard card-number check digit."""
    nums = [int(c) for c in digits if c.isdigit()]
    if len(nums) < 13:
        return False
    total = 0
    for i, d in enumerate(reversed(nums)):
        if i % 2:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


# --------------------------------------------------------------------------- #
# Government ID                                                                #
# --------------------------------------------------------------------------- #
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_GOV_ID_KEYWORD = re.compile(
    r"\b(?:social\s+security\s+(?:number|no\.?)|ssn"
    r"|national\s+insurance\s+(?:number|no\.?)"
    r"|passport\s+(?:number|no\.?)"
    r"|driver'?s?\s+licen[cs]e\s+(?:number|no\.?)"
    r"|driving\s+licen[cs]e\s+(?:number|no\.?)"
    r"|national\s+id(?:entity)?\s+(?:number|card)"
    r"|identity\s+card\s+(?:number|no\.?)"
    r"|teudat\s+zehut|aadhaar|tax\s+file\s+number"
    r"|taxpayer\s+identification\s+number)\b",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------- #
# Date of birth                                                                #
# --------------------------------------------------------------------------- #
_DOB = re.compile(
    r"\bdate\s+of\s+birth\b|\bd\.o\.b\.?\b|\bdob\b\s*[:=]|\bbirth\s?date\b"
    r"|\bborn\s+on\b|\bbirthday\s+is\b",
    re.IGNORECASE,
)


def _email_is_personal(text: str) -> bool:
    """True when *text* holds a consumer-mailbox email that is not exempt."""
    for match in _EMAIL.finditer(text):
        local, domain = match.group(1), match.group(2)
        if domain.lower() not in _CONSUMER_EMAIL_DOMAINS:
            continue  # corporate / example.com / test fixture — kept
        if local.lower().startswith(_EMAIL_EXEMPT_LOCAL):
            continue
        # Git authorship metadata is legitimate engineering content.
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start:line_end if line_end != -1 else len(text)]
        if _GIT_AUTHOR_LINE.search(line):
            continue
        return True
    return False


def _postal_address_present(text: str) -> bool:
    """True when *text* holds a postal address (not an IP/MAC/memory address)."""
    if _ADDRESS_KEYWORD.search(text) or _DWELLING.search(text):
        return True
    if _STREET_LINE.search(text):
        return True
    for match in _QUALIFIED_ADDRESS_IS.finditer(text):
        if match.group(1).lower() not in _TECHNICAL_ADDRESS_QUALIFIERS:
            return True
    return False


def _card_present(text: str) -> bool:
    if _PAYMENT_KEYWORD.search(text):
        return True
    for match in _CARD_GROUPED.finditer(text):
        if _luhn_ok(match.group(0)):
            return True
    return bool(_IBAN.search(text))


def find_pii(text: str) -> PIIFinding | None:
    """The first kind of personal data found in *text*, or ``None``.

    Order is stable so the reported ``kind`` is deterministic for a given input.
    """
    if not text or not text.strip():
        return None

    if _postal_address_present(text):
        return PIIFinding("street_address")
    if _PHONE_WITH_KEYWORD.search(text) or _E164.search(text):
        return PIIFinding("phone_number")
    if _email_is_personal(text):
        return PIIFinding("personal_email")
    if _card_present(text):
        return PIIFinding("payment_details")
    if _SSN.search(text) or _GOV_ID_KEYWORD.search(text):
        return PIIFinding("government_id")
    if _DOB.search(text):
        return PIIFinding("date_of_birth")
    return None


def contains_pii(*parts: str) -> PIIFinding | None:
    """:func:`find_pii` over several fields (title, content, …) at once."""
    for part in parts:
        found = find_pii(part or "")
        if found is not None:
            return found
    return None
