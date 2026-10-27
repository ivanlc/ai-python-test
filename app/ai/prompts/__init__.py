"""Load the extraction prompts from versioned text files.

Prompts live next to this module so that a prompt change is a reviewable text
diff, is shipped inside the `app/` image, and is read by the evaluator from the
same files. They are loaded once at import time: a missing or malformed file
stops the service at startup instead of failing individual requests.
"""

import hashlib
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent
REASON_PLACEHOLDER = "{reason}"


def _load(name: str) -> str:
    text = (PROMPT_DIR / name).read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"Prompt file {name} is empty")
    return text


SYSTEM_PROMPT = _load("extraction_system.txt")
_RETRY_TEMPLATE = _load("retry.txt")
if _RETRY_TEMPLATE.count(REASON_PLACEHOLDER) != 1:
    raise RuntimeError(f"retry.txt must contain {REASON_PLACEHOLDER} exactly once")

SYSTEM_PROMPT_SHA256 = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()


def retry_instructions(reason: str) -> str:
    """Text appended to the system prompt on a re-ask.

    `reason` must be a fixed guardrail message, never model or user text.
    """
    return "\n\n" + _RETRY_TEMPLATE.replace(REASON_PLACEHOLDER, reason)


__all__ = ["SYSTEM_PROMPT", "SYSTEM_PROMPT_SHA256", "retry_instructions"]
