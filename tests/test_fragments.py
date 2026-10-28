"""Adversarial check: a fragment of the intended body is never sent as the message.

Valid model output can only be checked against the request. For many phrasings,
every word-aligned fragment of the body, the mock's last-colon split and its
50-character prefix are submitted as the model's message. Each must either be
rejected or be repaired to the complete body; a wrong body must never pass.
"""

import itertools
import json

import pytest

from app.guardrails import ExtractionError, extract_notification

RECIPIENTS = [("ana@example.com", "email", "un email"), ("611-222-333", "sms", "un SMS")]
BODIES = [
    "la reunión es a las 10:30.",
    "Nos vemos el lunes a las 08:30",
    "Tu pedido 123 llegó: revisa el estado.",
    "¡Gracias por todo!",
]
TEMPLATES = [
    "Envía {ch} a {to}: {b}",
    "Envía {ch} a {to} diciendo {b}",
    "Envía {ch} a {to} diciendo que {b}",
    "Envía {ch} a {to} diciendo: {b}",
    "Envía {ch} a {to}, diciendo que {b}",
    "Avisa a {to} que {b}",
    "Avisa a {to} que: {b}",
    "Envía {ch} a {to} el mensaje: {b}",
    "Envía {ch} a {to} y dile que {b}",
    "Send {ch} to {to} saying: {b}",
    "Mensaje para {to} - {b}",
    "Por favor, cuando puedas y sin falta, avisa a {to} de que {b}",
    "Envía '{b}' a {to}",
    "{b} -> mándalo a {to}",
]


def _fragments(source: str, body: str) -> set:
    words = body.split()
    spans = {" ".join(words[i:j]) for i in range(len(words)) for j in range(i + 1, len(words) + 1)}
    lowered = source.lower()
    spans |= {lowered.split(":")[-1].strip(), lowered[:50], source}  # the mock's outputs
    spans.discard(body)
    return {span for span in spans if span}


@pytest.mark.parametrize("recipient, template", list(itertools.product(RECIPIENTS, TEMPLATES)))
def test_no_fragment_is_sent_as_the_body(recipient, template):
    to, channel, label = recipient
    for body in BODIES:
        source = template.format(ch=label, to=to, b=body)
        for fragment in _fragments(source, body):
            content = json.dumps({"to": to, "message": fragment, "type": channel}, ensure_ascii=False)
            try:
                sent = extract_notification(content, source).message
            except ExtractionError:
                continue
            assert sent.rstrip(".!") == body.rstrip(".!"), (source, fragment, sent)
