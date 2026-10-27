"""Bounded parsing, normalization and validation of model output."""

import ast
from io import StringIO
import json
import re
import tokenize
from typing import Any, Dict, List, Optional, Tuple

from email_validator import EmailNotValidError, validate_email

from .models import Notification


class ExtractionError(ValueError):
    """The model output cannot be trusted as a notification."""


# Capture whole address tokens so a malformed address cannot yield a valid suffix.
CONTACT_TOKEN_RE = re.compile(r'''[^\s<>\[\](){}"',;:]+''')
PHONE_RE = re.compile(r"[0-9]{3}-?[0-9]{3}-?[0-9]{3,4}")
PHONE_SOURCE_RE = re.compile(r"(?<![\w+-])[0-9]{3}-?[0-9]{3}-?[0-9]{3,4}(?![\w-])")
EMAIL_CHANNEL_RE = re.compile(r"\b(?:e-?mail|correo)\b", re.IGNORECASE)
SMS_CHANNEL_RE = re.compile(r"\b(?:sms|tel[eé]fono)\b", re.IGNORECASE)
UNSUPPORTED_CHANNEL_RE = re.compile(r"\b(?:whats\s*app|telegram|slack|fax|push)\b", re.IGNORECASE)
BODY_DESCRIPTION_RE = re.compile(r"^(?:(?:con|with)\s+)?(?:un|una|alg[uú]n|alguna|a|some)\s+(?:mensaje|texto|message|text)\b", re.I)
BODY_INTRODUCER_RE = re.compile(r"\b(?:diciendo(?:\s+que)?|que\s+diga|saying|con\s+el\s+texto|with\s+the\s+message)\s+", re.I)
REFUSAL_RE = re.compile(
    r"\b(?:refused|refusal|i cannot|i can['’]t|unable to|lo siento|no puedo|no tengo permitido)\b|\berror\s*:",
    re.IGNORECASE,
)
ALIASES = {
    "to": "to", "recipient": "to", "destination": "to",
    "message": "message", "body": "message", "text": "message",
    "type": "type", "channel": "type", "method": "type",
}


def _object_span(text: str) -> Tuple[int, int]:
    """Require one complete object; reject additional truncated objects too."""
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
    if depth or quote or len(objects) != 1:
        raise ExtractionError("Expected exactly one complete object")
    return objects[0]


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


def validate_notification(fields: Dict[str, str], user_input: str) -> Notification:
    """Ground the recipient in the request and require a coherent channel."""
    intent_input, explicit_body = _split_explicit_body(user_input)
    emails: Dict[str, str] = {}
    masked = list(intent_input)
    for match in CONTACT_TOKEN_RE.finditer(intent_input):
        if "@" not in match.group(0):
            continue
        address = match.group(0).rstrip(".!?")
        key = _email_key(address)
        if key is None:
            raise ExtractionError("The request contains an invalid email address")
        emails[key] = address
        masked[match.start():match.end()] = " " * (match.end() - match.start())
    # Digits in an email local part are not a second phone recipient.
    without_emails = "".join(masked)
    phone_matches = list(PHONE_SOURCE_RE.finditer(without_emails))
    phones = {match.group(0).replace("-", ""): match.group(0) for match in phone_matches}
    if len(emails) + len(phones) != 1:
        raise ExtractionError("A unique recipient is not present in the request")

    recipient = fields.get("to")
    if recipient is None:
        # Recovery uses the sole address explicitly supplied by the user.
        recipient = next(iter((emails or phones).values()))
    email = _email_key(recipient) in emails
    phone = bool(PHONE_RE.fullmatch(recipient)) and recipient.replace("-", "") in phones
    if not (email or phone):
        raise ExtractionError("Recipient is invalid or absent from the request")

    message = fields.get("message", "")
    if not message or len(message) > 4000:
        raise ExtractionError("Message is absent or oversized")
    if message.casefold() not in user_input.casefold():
        raise ExtractionError("Message is not grounded in the request")
    if message.casefold() == user_input.strip().casefold():
        raise ExtractionError("Message is the command rather than its body")
    if explicit_body is not None:
        if message.casefold() != explicit_body.casefold():
            raise ExtractionError("Message does not match the complete explicit body")
    elif BODY_DESCRIPTION_RE.match(message):
        quoted = any(f"{opening}{message}{closing}" in user_input
                     for opening, closing in [('"', '"'), ("'", "'"), ("«", "»")])
        introduced = any(user_input[match.end():].strip().casefold() == message.casefold()
                         for match in BODY_INTRODUCER_RE.finditer(user_input))
        if not (quoted or introduced):
            raise ExtractionError("Message is a description rather than explicit content")
    expected_channel = "email" if email else "sms"
    channel = fields.get("type", expected_channel).casefold()
    instruction = without_emails.split(":", 1)[0]
    if UNSUPPORTED_CHANNEL_RE.search(instruction):
        raise ExtractionError("Requested channel is unsupported")
    requested = set()
    if EMAIL_CHANNEL_RE.search(instruction):
        requested.add("email")
    if SMS_CHANNEL_RE.search(instruction):
        requested.add("sms")
    if channel != expected_channel or (requested and requested != {channel}):
        raise ExtractionError("Channel conflicts with recipient")
    return Notification(to=recipient, message=message, type=channel)


def extract_notification(content: str, user_input: str) -> Notification:
    return validate_notification(normalize_fields(parse_output(content)), user_input)
