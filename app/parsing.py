"""Bounded parsing, normalization and validation of model output."""

import ast
from dataclasses import dataclass
from io import StringIO
import json
import re
import tokenize
from typing import Any, Dict, FrozenSet, List, Literal, Optional, Tuple

from email_validator import EmailNotValidError, validate_email

from .models import Notification


class ExtractionError(ValueError):
    """The model output cannot be trusted as a notification."""


class RequestRejected(ExtractionError):
    """The request itself cannot yield a notification, whatever the model returns."""


class ModelDeclined(ExtractionError):
    """The model followed the refusal protocol: a deliberate decision, not noise."""


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
REFUSAL_RE = re.compile(
    r"\b(?:refused|refusal|i cannot|i can['’]t|unable to|lo siento|no puedo|no tengo permitido)\b|\berror\s*:",
    re.IGNORECASE,
)
ALIASES = {
    "to": "to", "recipient": "to", "destination": "to",
    "message": "message", "body": "message", "text": "message",
    "type": "type", "channel": "type", "method": "type",
}


def _scan_objects(text: str) -> Tuple[List[Tuple[int, int]], int, Optional[str], Optional[int]]:
    """Return complete top-level objects plus the scanner state at the end of text."""
    objects: List[Tuple[int, int]] = []
    start = None
    depth = 0
    quote = None
    escaped = False
    for index, char in enumerate(text):
        if quote:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if depth and char in ("'", '"'):
            quote = char
        elif char == "{":
            if depth == 0:
                start = index
            depth += 1
            if depth > 32:
                raise ExtractionError("Model output is too deeply nested")
        elif char == "}":
            if not depth:
                raise ExtractionError("Unmatched object delimiter")
            depth -= 1
            if depth == 0 and start is not None:
                objects.append((start, index + 1))
                start = None
    return objects, depth, quote, start


def _object_span(text: str) -> Tuple[int, int]:
    """Require one complete object; reject additional truncated objects too."""
    objects, depth, quote, _ = _scan_objects(text)
    if depth or quote or len(objects) != 1:
        raise ExtractionError("Expected exactly one complete object")
    return objects[0]


def _close_truncated_object(text: str) -> str:
    """Close a single flat object whose last string value is complete.

    Only the missing closing brace (optionally preceded by an ellipsis or a
    trailing comma) is added. Open strings, nested objects, a second object, or
    a dangling key are never completed, so no field value is synthesized.
    """
    objects, depth, quote, start = _scan_objects(text)
    if objects or depth != 1 or quote or start is None:
        raise ExtractionError("Expected exactly one complete object")
    tail = text[start:].rstrip()
    tail = re.sub(r"\s*(?:\.{3}|\u2026)$", "", tail).rstrip().rstrip(",").rstrip()
    if not tail.endswith(("'", '"')):
        raise ExtractionError("Truncated object cannot be closed safely")
    return text[:start] + tail + "}"


def _unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ExtractionError("Duplicate object keys")
        result[key] = value
    return result


def _repair_object(candidate: str) -> str:
    """Repair literal quotes and bare keys without rewriting string contents."""
    tokens = [token for token in tokenize.generate_tokens(StringIO(candidate).readline)
              if token.type not in (tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT)]
    repaired = []
    for index, token in enumerate(tokens):
        value = token.string
        if token.type == tokenize.STRING:
            # Only simple string literals are supported, never expressions or f-strings.
            if not value.startswith(("'", '"')):
                raise ExtractionError("Unsupported string literal")
            literal = ast.literal_eval(value)
            if not isinstance(literal, str):
                raise ExtractionError("Unsupported string literal")
            value = json.dumps(literal)
        elif (token.type == tokenize.NAME and index > 0 and index + 1 < len(tokens)
              and tokens[index - 1].string in ("{", ",")
              and tokens[index + 1].string == ":"):
            value = json.dumps(value)
        repaired.append((token.type, value))
    return tokenize.untokenize(repaired)


def parse_output(content: str) -> Dict[str, Any]:
    """Accept JSON, Python-style string quotes, and simple bare keys only."""
    if not isinstance(content, str) or len(content) > 12000:
        raise ExtractionError("Model output is absent or oversized")
    if "{" not in content:
        if content.strip().casefold() == "refused":
            raise ModelDeclined("Model refused the request")
        if REFUSAL_RE.search(content):
            raise ExtractionError("Model refused the request")
        raise ExtractionError("Model output contains no object")
    try:
        start, end = _object_span(content)
    except ExtractionError:
        # Recover only an object cut after its last complete value; validation
        # below still grounds every field in the user's request.
        content = _close_truncated_object(content)
        start, end = _object_span(content)
    if REFUSAL_RE.search(content[:start] + content[end:]):
        raise ExtractionError("Model refused the request")
    candidate = content[start:end]
    try:
        parsed = json.loads(candidate, object_pairs_hook=_unique_object)
    except json.JSONDecodeError:
        try:
            repaired = _repair_object(candidate)
            parsed = json.loads(repaired, object_pairs_hook=_unique_object)
        except (ValueError, SyntaxError, TypeError, tokenize.TokenError) as exc:
            raise ExtractionError("Model output is not a repairable object") from exc
    if not isinstance(parsed, dict):
        raise ExtractionError("Model output is not an object")
    return parsed


def normalize_fields(raw: Dict[str, Any]) -> Dict[str, str]:
    """Map known aliases; reject conflicting duplicate values."""
    normalized: Dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        canonical = ALIASES.get(key.strip().casefold())
        if canonical is None:
            continue
        if not isinstance(value, str):
            raise ExtractionError("A notification field is not text")
        clean = value.strip()
        if canonical in normalized and normalized[canonical] != clean:
            raise ExtractionError("Conflicting field aliases")
        normalized[canonical] = clean
    return normalized


def _email_key(address: str) -> Optional[str]:
    try:
        # Syntax validation only: no DNS/network work on the processing path.
        return validate_email(address, check_deliverability=False, allow_smtputf8=False).normalized.casefold()
    except EmailNotValidError:
        return None


def _split_explicit_body(user_input: str) -> Tuple[str, Optional[str]]:
    """Separate a recipient-adjacent colon before inspecting contacts in the body."""
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
            return user_input[:end], remainder[1:].strip()
    return user_input, None


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


@dataclass(frozen=True)
class _RequestFacts:
    intent_input: str
    explicit_body: Optional[str]
    emails: Dict[str, str]
    phones: Dict[str, str]
    contact_spans: List[Tuple[int, int]]
    expected_channel: Literal["email", "sms"]
    requested: FrozenSet[str]


def _analyze_request(user_input: str) -> _RequestFacts:
    """Facts that depend only on the request; every failure here is model-independent."""
    intent_input, explicit_body = _split_explicit_body(user_input)
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
    return _RequestFacts(intent_input, explicit_body, emails, phones, contact_spans,
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
        # or its full text when the model split at a later colon inside it.
        if not (message.casefold() == explicit_body.casefold()
                or _is_inner_colon_suffix(message, explicit_body)
                or _is_unquoted_body(message, explicit_body)):
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


def extract_notification(content: str, user_input: str) -> Notification:
    return validate_notification(normalize_fields(parse_output(content)), user_input)
