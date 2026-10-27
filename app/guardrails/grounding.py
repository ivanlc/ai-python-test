"""Ground candidate fields in the user's request (content stage).

The request is the source of truth: every accepted value must be read from it.
"""

from dataclasses import dataclass
import re
from typing import Dict, FrozenSet, List, Literal, Optional, Tuple

from email_validator import EmailNotValidError, validate_email

from ..models import Notification
from .output import ExtractionError


class RequestRejected(ExtractionError):
    """The request itself cannot yield a notification, whatever the model returns."""



# Capture whole address tokens so a malformed address cannot yield a valid suffix.
CONTACT_TOKEN_RE = re.compile(r'''[^\s<>\[\](){}"',;:]+''')
PHONE_RE = re.compile(r"[0-9]{3}-?[0-9]{3}-?[0-9]{3,4}")
PHONE_SOURCE_RE = re.compile(r"(?<![\w+-])[0-9]{3}-?[0-9]{3}-?[0-9]{3,4}(?![\w-])")
EMAIL_CHANNEL_RE = re.compile(r"\b(?:e-?mail|correo)\b", re.IGNORECASE)
SMS_CHANNEL_RE = re.compile(r"\b(?:sms|tel[eé]fono)\b", re.IGNORECASE)
UNSUPPORTED_CHANNEL_RE = re.compile(r"\b(?:whats\s*app|telegram|slack|fax|push)\b", re.IGNORECASE)
BODY_DESCRIPTION_RE = re.compile(
    r"^(?:(?:con|with)\s+)?(?:un|una|alg[uú]n|alguna|a|some)\s+(?:mensaje|texto|message|text)\b", re.I)
BODY_INTRODUCER_RE = re.compile(
    r"\b(?:diciendo(?:\s+que)?|que\s+diga|saying|con\s+el\s+texto|with\s+the\s+message)\s+", re.I)
BODY_CONNECTOR_RE = re.compile(
    r"(?:diciendo(?:\s+que)?|que\s+diga|que|saying(?:\s+that)?|con\s+el\s+texto|with\s+the\s+message)\s+", re.I)

def _email_key(address: str) -> Optional[str]:
    try:
        # Syntax validation only: no DNS/network work on the processing path.
        return validate_email(address, check_deliverability=False, allow_smtputf8=False).normalized.casefold()
    except EmailNotValidError:
        return None


def _split_explicit_body(user_input: str) -> Tuple[str, Optional[str], Optional[str]]:
    """Separate a recipient-adjacent delimiter before inspecting the body as data."""
    contacts = []
    masked = list(user_input)
    for match in CONTACT_TOKEN_RE.finditer(user_input):
        if "@" in match.group(0):
            contacts.append((match.start(), match.end()))
            masked[match.start():match.end()] = " " * (match.end() - match.start())
    contacts.extend((match.start(), match.end())
                    for match in PHONE_SOURCE_RE.finditer("".join(masked)))
    for _, end in sorted(contacts):
        remainder = user_input[end:].lstrip()
        if remainder.startswith(":"):
            return user_input[:end], remainder[1:].strip(), None
        connector = BODY_CONNECTOR_RE.match(remainder)
        if connector:
            return user_input[:end], remainder[connector.end():].strip(), connector.group().strip()
    return user_input, None, None


def _source_spans(message: str, user_input: str) -> List[Tuple[int, int]]:
    """Locate the message in the request, ignoring only letter case."""
    return [(match.start(), match.end())
            for match in re.finditer(re.escape(message), user_input, re.IGNORECASE)]


def _contains_contact(text: str) -> bool:
    return (any("@" in match.group(0) for match in CONTACT_TOKEN_RE.finditer(text))
            or PHONE_SOURCE_RE.search(text) is not None)


def _is_inner_colon_suffix(fragment: str, body: str) -> bool:
    """Detect a body split at a later colon, e.g. `00` from `cita a las 10:00`.

    Repair is limited to a contact-free body: a later recipient in the body may
    signal a second notification that must not be merged into this one.
    """
    match = re.search(re.escape(fragment.strip()) + r"\Z", body, re.IGNORECASE)
    if match is None or match.start() == 0:
        return False
    return body[:match.start()].rstrip().endswith(":") and not _contains_contact(body)


QUOTE_PAIRS = {'"': '"', "'": "'", "\u00ab": "\u00bb", "\u201c": "\u201d"}


def _is_unquoted_body(message: str, body: str) -> bool:
    """Accept the exact text inside quotes that wrap the whole explicit body."""
    if len(body) < 3 or QUOTE_PAIRS.get(body[0]) != body[-1]:
        return False
    return message.strip().casefold() == body[1:-1].strip().casefold()


def _includes_body_connector(message: str, body: str, connector: Optional[str]) -> bool:
    """Remove only the source delimiter accidentally included before the full body."""
    if connector is None:
        return False
    words = connector.split()
    # A model may retain all or a trailing part of "diciendo que" / "saying that".
    return any(message.casefold() == (" ".join(words[index:]) + " " + body).casefold()
               for index in range(len(words)))


@dataclass(frozen=True)
class _RequestFacts:
    intent_input: str
    explicit_body: Optional[str]
    body_connector: Optional[str]
    emails: Dict[str, str]
    phones: Dict[str, str]
    contact_spans: List[Tuple[int, int]]
    expected_channel: Literal["email", "sms"]
    requested: FrozenSet[str]


def _analyze_request(user_input: str) -> _RequestFacts:
    """Facts that depend only on the request; every failure here is model-independent."""
    intent_input, explicit_body, body_connector = _split_explicit_body(user_input)
    emails: Dict[str, str] = {}
    contact_spans: List[Tuple[int, int]] = []
    masked = list(intent_input)
    for match in CONTACT_TOKEN_RE.finditer(intent_input):
        if "@" not in match.group(0):
            continue
        address = match.group(0).rstrip(".!?")
        key = _email_key(address)
        if key is None:
            raise RequestRejected("The request contains an invalid email address")
        emails[key] = address
        contact_spans.append((match.start(), match.end()))
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    # Digits in an email local part are not a second phone recipient.
    without_emails = "".join(masked)
    phone_matches = list(PHONE_SOURCE_RE.finditer(without_emails))
    phones = {match.group(0).replace("-", ""): match.group(0) for match in phone_matches}
    contact_spans += [(match.start(), match.end()) for match in phone_matches]
    if len(emails) + len(phones) != 1:
        raise RequestRejected("A unique recipient is not present in the request")
    # A copied JSON example is not a notification instruction. A contact inside
    # an object cannot establish intent, even when every model field is a source
    # substring. Bodies after an actual recipient remain opaque data.
    for start, _ in contact_spans:
        depth = 0
        for char in intent_input[:start]:
            if char == "{":
                depth += 1
            elif char == "}":
                depth = max(0, depth - 1)
        if depth:
            raise RequestRejected("Recipient appears in structured data rather than a notification instruction")
    expected_channel: Literal["email", "sms"] = "email" if emails else "sms"
    instruction = without_emails.split(":", 1)[0]
    if UNSUPPORTED_CHANNEL_RE.search(instruction):
        raise RequestRejected("Requested channel is unsupported")
    requested = set()
    if EMAIL_CHANNEL_RE.search(instruction):
        requested.add("email")
    if SMS_CHANNEL_RE.search(instruction):
        requested.add("sms")
    if requested and requested != {expected_channel}:
        raise RequestRejected("Requested channel conflicts with the recipient")
    return _RequestFacts(intent_input, explicit_body, body_connector, emails, phones, contact_spans,
                         expected_channel, frozenset(requested))


def check_request(user_input: str) -> None:
    """Pre-flight check: reject before calling the model when no output could pass."""
    _analyze_request(user_input)


def validate_notification(fields: Dict[str, str], user_input: str) -> Notification:
    """Ground every field in the request; request-level failures raise RequestRejected."""
    facts = _analyze_request(user_input)
    explicit_body = facts.explicit_body

    recipient = fields.get("to")
    if recipient is None:
        # Recovery uses the sole address explicitly supplied by the user.
        recipient = next(iter((facts.emails or facts.phones).values()))
    email = _email_key(recipient) in facts.emails
    phone = bool(PHONE_RE.fullmatch(recipient)) and recipient.replace("-", "") in facts.phones
    if not (email or phone):
        raise ExtractionError("Recipient is invalid or absent from the request")

    message = fields.get("message", "")
    if not message or len(message) > 4000:
        raise ExtractionError("Message is absent or oversized")
    if message.casefold() == user_input.strip().casefold():
        raise ExtractionError("Message is the command rather than its body")
    spans = _source_spans(message, user_input)
    if not spans:
        raise ExtractionError("Message is not grounded in the request")
    if explicit_body is not None:
        # The delimited source text is authoritative: restore its exact casing,
        # or its full text when the model split at a later colon inside it. That
        # repair is limited to colon delimiters: after a connector such as "que",
        # a later colon may end a relative clause ("a pedro@x.com que está de
        # vacaciones: vuelve el lunes"), so the text before it is not the body.
        if not (message.casefold() == explicit_body.casefold()
                or (facts.body_connector is None and _is_inner_colon_suffix(message, explicit_body))
                or _is_unquoted_body(message, explicit_body)
                or _includes_body_connector(message, explicit_body, facts.body_connector)
                or message.casefold() == explicit_body.rstrip(".!?\u2026").casefold()):
            raise ExtractionError("Message does not match the complete explicit body")
        message = explicit_body
    else:
        body_spans = [(start, end) for start, end in spans
                      if not any(start < c_end and c_start < end for c_start, c_end in facts.contact_spans)]
        if not body_spans:
            raise ExtractionError("Message includes the command or recipient rather than only its body")
        # Grounding is case-insensitive; send the user's original casing, and keep
        # final sentence punctuation that a model dropped at the end of the request.
        start, end = body_spans[0]
        if re.fullmatch(r"[.!?\u2026]+", user_input[end:].rstrip()):
            end = len(user_input.rstrip())
        message = user_input[start:end]
    if explicit_body is None and BODY_DESCRIPTION_RE.match(message):
        quoted = any(f"{opening}{message}{closing}" in user_input
                     for opening, closing in [('"', '"'), ("'", "'"), ("«", "»")])
        introduced = any(user_input[match.end():].strip().casefold() == message.casefold()
                         for match in BODY_INTRODUCER_RE.finditer(user_input))
        if not (quoted or introduced):
            raise ExtractionError("Message is a description rather than explicit content")
    channel = fields.get("type", facts.expected_channel).casefold()
    if channel != facts.expected_channel:
        raise ExtractionError("Channel conflicts with recipient")
    return Notification(to=recipient, message=message, type=facts.expected_channel)
