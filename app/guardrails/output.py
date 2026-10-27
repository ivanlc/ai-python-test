"""Parse and repair raw model output into candidate fields (format stage)."""

import ast
from io import StringIO
import json
import re
import tokenize
from typing import Any, Dict, List, Optional, Tuple


class ExtractionError(ValueError):
    """The model output cannot be trusted as a notification."""


class OutputFormatError(ExtractionError):
    """The output is not a usable answer (off-protocol refusal, no or broken object).

    This is the noise a second sample can fix, so the pipeline re-asks once.
    Content that parses but disagrees with the request is not re-asked: a
    deterministic model would repeat it.
    """


class ModelDeclined(ExtractionError):
    """The model followed the refusal protocol: a deliberate decision, not noise."""


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
                raise OutputFormatError("Model output is too deeply nested")
        elif char == "}":
            if not depth:
                raise OutputFormatError("Unmatched object delimiter")
            depth -= 1
            if depth == 0 and start is not None:
                objects.append((start, index + 1))
                start = None
    return objects, depth, quote, start


def _object_span(text: str) -> Tuple[int, int]:
    """Require one complete object; reject additional truncated objects too."""
    objects, depth, quote, _ = _scan_objects(text)
    if depth or quote or len(objects) != 1:
        raise OutputFormatError("Expected exactly one complete object")
    return objects[0]


def _close_truncated_object(text: str) -> str:
    """Close a single flat object whose last string value is complete.

    Only the missing closing brace (optionally preceded by an ellipsis or a
    trailing comma) is added. Open strings, nested objects, a second object, or
    a dangling key are never completed, so no field value is synthesized.
    """
    objects, depth, quote, start = _scan_objects(text)
    if objects or depth != 1 or quote or start is None:
        raise OutputFormatError("Expected exactly one complete object")
    tail = text[start:].rstrip()
    tail = re.sub(r"\s*(?:\.{3}|\u2026)$", "", tail).rstrip().rstrip(",").rstrip()
    if not tail.endswith(("'", '"')):
        raise OutputFormatError("Truncated object cannot be closed safely")
    return text[:start] + tail + "}"


def _unique_object(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise OutputFormatError("Duplicate object keys")
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
                raise OutputFormatError("Unsupported string literal")
            literal = ast.literal_eval(value)
            if not isinstance(literal, str):
                raise OutputFormatError("Unsupported string literal")
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
        raise OutputFormatError("Model output is absent or oversized")
    if "{" not in content:
        if content.strip().casefold() == "refused":
            raise ModelDeclined("Model refused the request")
        if REFUSAL_RE.search(content):
            raise OutputFormatError("Model refused the request")
        raise OutputFormatError("Model output contains no object")
    try:
        start, end = _object_span(content)
    except ExtractionError:
        # Recover only an object cut after its last complete value; validation
        # below still grounds every field in the user's request.
        content = _close_truncated_object(content)
        start, end = _object_span(content)
    if REFUSAL_RE.search(content[:start] + content[end:]):
        raise OutputFormatError("Model refused the request")
    candidate = content[start:end]
    try:
        parsed = json.loads(candidate, object_pairs_hook=_unique_object)
    except json.JSONDecodeError:
        try:
            repaired = _repair_object(candidate)
            parsed = json.loads(repaired, object_pairs_hook=_unique_object)
        except (ValueError, SyntaxError, TypeError, tokenize.TokenError) as exc:
            raise OutputFormatError("Model output is not a repairable object") from exc
    if not isinstance(parsed, dict):
        raise OutputFormatError("Model output is not an object")
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
            raise OutputFormatError("A notification field is not text")
        clean = value.strip()
        if canonical in normalized and normalized[canonical] != clean:
            raise OutputFormatError("Conflicting field aliases")
        normalized[canonical] = clean
    return normalized
