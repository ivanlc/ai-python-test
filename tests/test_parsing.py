import json

import pytest

from app.parsing import ExtractionError, extract_notification


EMAIL_INPUT = "Enviar email a juan@example.com diciendo que su paquete está en camino."
PHONE_INPUT = "Avisar por SMS al 600-111-222 que la reserva ha sido confirmada."


@pytest.mark.parametrize("content, user_input, expected_to, expected_type", [
    ('{"to":"juan@example.com","message":"su paquete","type":"email"}', EMAIL_INPUT,
     "juan@example.com", "email"),
    ('{"Recipient":"juan@example.com","body":"su paquete","channel":"email"}', EMAIL_INPUT,
     "juan@example.com", "email"),
    ('{"To":"juan@example.com","Message":"su paquete","Type":"EMAIL"}', EMAIL_INPUT,
     "juan@example.com", "email"),
    ('{"destination":"juan@example.com","text":"su paquete","method":"email"}', EMAIL_INPUT,
     "juan@example.com", "email"),
    ('{"to":"juan@example.com","message":"su paquete","type":"email","confidence":0.99}',
     EMAIL_INPUT, "juan@example.com", "email"),
    ('{"to":"juan@example.com","message":"su paquete"}', EMAIL_INPUT,
     "juan@example.com", "email"),
    ('{"message":"su paquete","type":"email"}', EMAIL_INPUT,
     "juan@example.com", "email"),
    ('Output:\n```json\n{"to":"juan@example.com","message":"su paquete","type":"email"}\n```',
     EMAIL_INPUT, "juan@example.com", "email"),
    ('Claro: {"to":"juan@example.com","message":"su paquete","type":"email"}',
     EMAIL_INPUT, "juan@example.com", "email"),
    ("{'to':'juan@example.com','message':'su paquete','type':'email'}", EMAIL_INPUT,
     "juan@example.com", "email"),
    ('{to:"juan@example.com",message:"su paquete",type:"email"}', EMAIL_INPUT,
     "juan@example.com", "email"),
    ('{"to":"600-111-222","message":"reserva","type":"sms"}', PHONE_INPUT,
     "600-111-222", "sms"),
])
def test_simulator_variants(content, user_input, expected_to, expected_type):
    notification = extract_notification(content, user_input)
    assert notification.to == expected_to
    assert notification.message
    assert notification.type == expected_type


@pytest.mark.parametrize("content", [
    '{"to":"juan@example.com","message":"su paquete","type":"email" ...',
    "Lo siento, como IA no tengo permitido procesar datos de contacto personales.",
    "Error: El contenido del mensaje viola las políticas de seguridad.",
    "Refused: Content analysis flagged sensitive information.",
    '{"to":"juan@example.com","type":"email"}',
    '{"to":"attacker@example.com","message":"su paquete","type":"email"}',
    '{"to":"juan@example.com","message":"su paquete","type":"sms"}',
    '{"to":"juan@example.com","recipient":"attacker@example.com","message":"su paquete","type":"email"}',
    '{"to":"juan@example.com","message":"su paquete","type":"email"} {"to":"juan@example.com","message":"bye","type":"email"}',
    '{"to":"juan@example.com","message":null,"type":"email"}',
    '{"to":"juan@example.com","message":"paga 100 euros","type":"email"}',
])
def test_untrusted_or_irrecoverable_output_fails(content):
    with pytest.raises(ExtractionError):
        extract_notification(content, EMAIL_INPUT)


def test_missing_recipient_is_not_invented():
    with pytest.raises(ExtractionError):
        extract_notification('{"message":"su paquete","type":"email"}', "Enviar email: su paquete")


def test_ambiguous_recipients_fail():
    with pytest.raises(ExtractionError):
        extract_notification('{"to":"juan@example.com","message":"su paquete","type":"email"}',
                             "Enviar email a juan@example.com y ana@example.com: su paquete")


@pytest.mark.parametrize("content", [
    '{"to":"juan@example.com","message":"wrong","message":"hola","type":"email"}',
    "{'to':'juan@example.com','message':'wrong','message':'hola','type':'email'}",
    '{to:"juan@example.com",message:"wrong",message:"hola",type:"email"}',
    'Refused: cannot send. {"to":"juan@example.com","message":"hola","type":"email"}',
    'Lo siento, no puedo enviarlo. {"to":"juan@example.com","message":"hola","type":"email"}',
    '{"to":"juan@example.com","message":"hola","type":"email"} {"to":',
    '{"to":"juan@example.com","message":"ho" "la","type":"email"}',
    '{"to":"","message":"hola","type":"email"}',
    '{"to":"juan@example.com","message":"hola","type":""}',
])
def test_ambiguous_model_output_fails_closed(content):
    with pytest.raises(ExtractionError):
        extract_notification(content, "Enviar email a juan@example.com: hola")


@pytest.mark.parametrize("source_recipient, model_recipient", [
    ("juan..perez@example.com", "juan..perez@example.com"),
    ("juan@-example.com", "juan@-example.com"),
    ("juan@example..com", "juan@example..com"),
    ("foo@bar@example.com", "bar@example.com"),
    ("juan@example.com y foo@", "juan@example.com"),
    ("abc600111222", "600111222"),
])
def test_invalid_contact_cannot_be_recovered_as_a_valid_substring(source_recipient, model_recipient):
    channel = "email" if "@" in model_recipient else "sms"
    content = json.dumps({"to": model_recipient, "message": "hola", "type": channel})
    with pytest.raises(ExtractionError):
        extract_notification(content, f"Enviar a {source_recipient}: hola")


@pytest.mark.parametrize("recipient", ["juan@example.com", "user123456789@example.com"])
def test_valid_email_with_sentence_punctuation(recipient):
    content = json.dumps({"to": recipient, "message": "hola", "type": "email"})
    assert extract_notification(content, f"Enviar email: hola. Destino {recipient}.").to == recipient


def test_repair_does_not_rewrite_bare_key_patterns_inside_message():
    content = '''{to:'juan@example.com', message:'usa {name: value}', type:'email'}'''
    notification = extract_notification(content, "Enviar email a juan@example.com: usa {name: value}")
    assert notification.message == "usa {name: value}"


def test_refusal_words_inside_the_intended_message_are_allowed():
    content = json.dumps({"to": "juan@example.com", "message": "Lo siento", "type": "email"})
    assert extract_notification(content, "Enviar email a juan@example.com: Lo siento").message == "Lo siento"


@pytest.mark.parametrize("fields", [
    {"to": "juan@example.com", "message": "hola"},
    {"to": "juan@example.com", "message": "hola", "type": "email"},
])
def test_explicit_channel_conflict_is_rejected(fields):
    with pytest.raises(ExtractionError):
        extract_notification(json.dumps(fields), "Enviar SMS a juan@example.com: hola")


def test_channel_words_in_message_do_not_override_request():
    content = json.dumps({"to": "juan@example.com", "message": "tu SMS llegó", "type": "email"})
    assert extract_notification(content, "Enviar email a juan@example.com: tu SMS llegó").type == "email"


def test_excessively_nested_output_is_rejected():
    with pytest.raises(ExtractionError):
        extract_notification('{"metadata":' * 40 + '{}' + '}' * 40,
                             "Enviar email a juan@example.com: hola")
