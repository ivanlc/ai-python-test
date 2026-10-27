"""Guardrails between the model and the notification provider.

`output` turns raw text into candidate fields; `grounding` checks them against
the user's request. Nothing that fails here reaches `/v1/notify`.
"""

from ..models import Notification
from .grounding import RequestRejected, check_request, validate_notification
from .output import ExtractionError, ModelDeclined, OutputFormatError, normalize_fields, parse_output


def extract_notification(content: str, user_input: str) -> Notification:
    return validate_notification(normalize_fields(parse_output(content)), user_input)


__all__ = ["ExtractionError", "ModelDeclined", "OutputFormatError", "RequestRejected", "check_request",
           "extract_notification", "normalize_fields", "parse_output", "validate_notification"]
