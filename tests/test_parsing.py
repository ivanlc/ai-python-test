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
