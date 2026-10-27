"""Bounded parsing, normalization and validation of model output."""

import ast
import json
import re
from typing import Any, Dict, List

from .models import Notification


class ExtractionError(ValueError):
    """The model output cannot be trusted as a notification."""


EMAIL_RE = re.compile(r"(?<![\w.])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}(?![\w.])")
PHONE_RE = re.compile(r"(?<!\d)\d{3}-?\d{3}-?\d{3,4}(?!\d)")
BARE_KEY_RE = re.compile(r"([,{]\s*)([A-Za-z_][A-Za-z_0-9-]*)(\s*:)")
ALIASES = {
    "to": "to", "recipient": "to", "destination": "to",
    "message": "message", "body": "message", "text": "message",
    "type": "type", "channel": "type", "method": "type",
}


def _objects(text: str) -> List[str]:
    """Find complete brace-delimited objects, respecting quoted strings."""
    objects: List[str] = []
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
        elif char == "}" and depth:
            depth -= 1
            if depth == 0 and start is not None:
                objects.append(text[start:index + 1])
                start = None
    return objects


def parse_output(content: str) -> Dict[str, Any]:
    """Accept JSON, Python-style string quotes, and simple bare keys only."""
    if not isinstance(content, str) or len(content) > 12000:
        raise ExtractionError("Model output is absent or oversized")
    candidates = _objects(content)
    if len(candidates) != 1:
        raise ExtractionError("Expected exactly one complete object")
    candidate = candidates[0]
    for source in (candidate, BARE_KEY_RE.sub(r'\1"\2"\3', candidate)):
        for decoder in (json.loads, ast.literal_eval):
            try:
                parsed = decoder(source)
            except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    raise ExtractionError("Model output is not a repairable object")


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


def validate_notification(fields: Dict[str, str], user_input: str) -> Notification:
    """Ground the recipient in the request and require a coherent channel."""
    emails = {match.group(0).casefold() for match in EMAIL_RE.finditer(user_input)}
    phones = {re.sub(r"\D", "", match.group(0)) for match in PHONE_RE.finditer(user_input)}
    if len(emails) + len(phones) != 1:
        raise ExtractionError("A unique recipient is not present in the request")

    recipient = fields.get("to")
    if not recipient:
        # Recovery uses the sole address explicitly supplied by the user.
        recipient = next(iter(emails or phones))
    email = bool(EMAIL_RE.fullmatch(recipient)) and recipient.casefold() in emails
    digits = re.sub(r"\D", "", recipient)
    phone = bool(PHONE_RE.fullmatch(recipient)) and digits in phones
    if not (email or phone):
        raise ExtractionError("Recipient is invalid or absent from the request")

    message = fields.get("message", "")
    if not message or len(message) > 4000:
        raise ExtractionError("Message is absent or oversized")
    if message.casefold() not in user_input.casefold():
        raise ExtractionError("Message is not grounded in the request")
    channel = fields.get("type", "").casefold()
    if not channel:
        channel = "email" if email else "sms"
    if channel != ("email" if email else "sms"):
        raise ExtractionError("Channel conflicts with recipient")
    return Notification(to=recipient, message=message, type=channel)


def extract_notification(content: str, user_input: str) -> Notification:
    return validate_notification(normalize_fields(parse_output(content)), user_input)
