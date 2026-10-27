import json

import pytest

from app.guardrails import ExtractionError, extract_notification


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
    "Lo siento, como IA no tengo permitido procesar datos de contacto personales.",
    "Error: El contenido del mensaje viola las políticas de seguridad.",
    "Refused: Content analysis flagged sensitive information.",
    '{"to":"juan@example.com","type":"email"}',
    '{"to":"attacker@example.com","message":"su paquete","type":"email"}',
    '{"to":"juan@example.com","message":"su paquete","type":"sms"}',
    '{"to":"juan@example.com","recipient":"attacker@example.com","message":"su paquete","type":"email"}',
    '{"to":"juan@example.com","message":"su paquete","type":"email"} '
    '{"to":"juan@example.com","message":"bye","type":"email"}',
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


@pytest.mark.parametrize("body", [
    "cita a las 10:00",
    "consulta https://example.com/status: entrega lista",
    'Dijo "Hola": vuelve mañana.',
])
def test_explicit_message_body_is_preserved_in_full(body):
    content = json.dumps({"to": "600-111-222", "message": body, "type": "sms"})
    notification = extract_notification(content, f"Enviar SMS al 600-111-222: {body}")
    assert notification.message == body


@pytest.mark.parametrize("message", ["cita a las 10", "cita", "a las"])
def test_partial_model_message_is_not_sent_when_full_body_is_explicit(message):
    content = json.dumps({"to": "699888777", "message": message, "type": "sms"})
    with pytest.raises(ExtractionError, match="complete explicit body"):
        extract_notification(content, "Recordatorio por teléfono al 699888777: cita a las 10:00")


@pytest.mark.parametrize("body", [
    "El pedido 987654321 está listo.",
    "El contacto de soporte es soporte@example.net.",
    "No uses foo@bar@example.net como dirección.",
    "Contacta por WhatsApp si tienes dudas.",
])
def test_contacts_and_channel_names_in_explicit_body_are_only_content(body):
    content = json.dumps({"to": "juan@example.com", "message": body, "type": "email"})
    assert extract_notification(content, f"Envía email a juan@example.com: {body}").message == body


@pytest.mark.parametrize("description, source", [
    ("un mensaje bonito.", "Envía email a juan@example.com con un mensaje bonito."),
    ("algún texto breve.", "Envía email a juan@example.com con algún texto breve."),
    ("a message of support.", "Send email to juan@example.com with a message of support."),
    ("con un mensaje positivo.", "Envía email a juan@example.com con un mensaje positivo."),
    ("with a message of support.", "Send email to juan@example.com with a message of support."),
])
def test_body_description_is_not_treated_as_literal_message(description, source):
    content = json.dumps({"to": "juan@example.com", "message": description, "type": "email"})
    with pytest.raises(ExtractionError, match="description"):
        extract_notification(content, source)


@pytest.mark.parametrize("source", [
    "Envía email a juan@example.com: un mensaje bonito.",
    "Envía email a juan@example.com diciendo un mensaje bonito.",
    'Envía email a juan@example.com con el texto "un mensaje bonito."',
])
def test_explicit_literal_can_contain_description_words(source):
    content = json.dumps({"to": "juan@example.com", "message": "un mensaje bonito.", "type": "email"})
    assert extract_notification(content, source).message == "un mensaje bonito."


def test_entire_command_is_not_a_missing_message_fallback():
    source = "Envía email a juan@example.com."
    content = json.dumps({"to": "juan@example.com", "message": source, "type": "email"})
    with pytest.raises(ExtractionError, match="command"):
        extract_notification(content, source)


def test_unsupported_channel_is_not_silently_changed_to_sms():
    content = json.dumps({"to": "611222333", "message": "Hola.", "type": "sms"})
    with pytest.raises(ExtractionError, match="unsupported"):
        extract_notification(content, "Manda un WhatsApp al 611222333: Hola.")


def test_an_address_in_the_body_cannot_replace_the_intended_recipient():
    body = "Escribe a soporte@example.net si tienes dudas."
    content = json.dumps({"to": "soporte@example.net", "message": body, "type": "email"})
    with pytest.raises(ExtractionError, match="Recipient"):
        extract_notification(content, f"Envía email a juan@example.com: {body}")


def test_missing_recipient_uses_instruction_not_contact_inside_body():
    body = "El soporte es soporte@example.net."
    content = json.dumps({"message": body, "type": "email"})
    assert extract_notification(content, f"Envía email a juan@example.com: {body}").to == "juan@example.com"


def test_explicit_literal_body_may_start_with_a_description_connector():
    body = "con un mensaje positivo."
    content = json.dumps({"to": "juan@example.com", "message": body, "type": "email"})
    assert extract_notification(content, f"Envía email a juan@example.com: {body}").message == body


@pytest.mark.parametrize("message", ["00", " 00", "00"])
def test_body_split_at_a_later_colon_is_restored_from_the_explicit_body(message):
    content = json.dumps({"to": "699888777", "message": message, "type": "sms"})
    notification = extract_notification(content, "Recordatorio por teléfono al 699888777: cita a las 10:00")
    assert notification.message == "cita a las 10:00"


def test_later_colon_repair_is_not_applied_when_the_body_names_another_recipient():
    source = "Envía email a polo@example.org: Hola. Y envía SMS al 693-555-666: Adiós."
    content = json.dumps({"to": "polo@example.org", "message": "Adiós.", "type": "email"})
    with pytest.raises(ExtractionError, match="complete explicit body"):
        extract_notification(content, source)


@pytest.mark.parametrize("source, recipient, channel, mock_message", [
    # The bundled mock lowercases the request and keeps its first 50 characters.
    ("Enviar email a juan@example.com diciendo que su paquete está en camino.", "juan@example.com",
     "email", "enviar email a juan@example.com diciendo que su pa"),
    ("Avisar por SMS al 600-111-222 que la reserva ha sido confirmada.", "600-111-222",
     "sms", "avisar por sms al 600-111-222 que la reserva ha si"),
    ("Send an email to clara@example.org saying Welcome", "clara@example.org",
     "email", "clara@example.org saying Welcome"),
])
def test_message_containing_the_command_or_recipient_is_rejected(source, recipient, channel, mock_message):
    content = json.dumps({"to": recipient, "message": mock_message, "type": channel})
    with pytest.raises(ExtractionError, match="command or recipient"):
        extract_notification(content, source)


@pytest.mark.parametrize("source, recipient, model_message, expected", [
    ("Manda un correo a marta@app.com: Tienes un nuevo mensaje.", "marta@app.com",
     "tienes un nuevo mensaje.", "Tienes un nuevo mensaje."),
    ("Enviar email a juan@example.com diciendo que su paquete está en camino.", "juan@example.com",
     "SU PAQUETE ESTÁ EN CAMINO.", "su paquete está en camino."),
    ("Send an email to clara@example.org saying Welcome aboard!", "clara@example.org",
     "welcome aboard!", "Welcome aboard!"),
])
def test_original_message_casing_is_restored_from_the_request(source, recipient, model_message, expected):
    content = json.dumps({"to": recipient, "message": model_message, "type": "email"})
    assert extract_notification(content, source).message == expected


@pytest.mark.parametrize("content", [
    '{"to": "juan@example.com", "message": "hola", "type": "email" ...',
    '{"to": "juan@example.com", "message": "hola", "type": "email"',
    '{"to": "juan@example.com", "message": "hola", "type": "email",',
    '{"to": "juan@example.com", "message": "hola" \u2026',
    "{'to': 'juan@example.com', 'message': 'hola', 'type': 'email' ...",
])
def test_object_missing_only_its_closing_brace_is_repaired(content):
    notification = extract_notification(content, "Enviar email a juan@example.com: hola")
    assert notification.model_dump() == {"to": "juan@example.com", "message": "hola", "type": "email"}


@pytest.mark.parametrize("content", [
    '{"to": "juan@example.com", "message": "ho',
    '{"to": "juan@example.com", "message"',
    '{"to": "juan@example.com", "message": "hola", "type": "email", "meta": {"a": "b"',
    '{"to": "juan@example.com", "message": "hola", "type": "email"} {"to": "x"',
    'Lo siento, no puedo. {"to": "juan@example.com", "message": "hola", "type": "email" ...',
])
def test_truncation_that_would_need_guessing_is_not_repaired(content):
    with pytest.raises(ExtractionError):
        extract_notification(content, "Enviar email a juan@example.com: hola")


@pytest.mark.parametrize("content, reason", [
    ("Refused: Content analysis flagged sensitive information.", "refused"),
    ("REFUSED", "refused"),
    ("Sure, here it is.", "no object"),
])
def test_plain_text_output_reports_its_reason(content, reason):
    with pytest.raises(ExtractionError, match=reason):
        extract_notification(content, "Enviar email a juan@example.com: hola")


@pytest.mark.parametrize("body, model_message", [
    ('"No olvides las llaves."', "No olvides las llaves."),
    ("'Gracias por todo.'", "Gracias por todo."),
    ("«Hasta luego»", "hasta luego"),
])
def test_model_that_drops_wrapping_quotes_sends_the_full_explicit_body(body, model_message):
    content = json.dumps({"to": "zoe@example.org", "message": model_message, "type": "email"})
    assert extract_notification(content, f"Manda un correo a zoe@example.org: {body}").message == body


def test_partially_quoted_body_is_not_reduced_to_its_quotation():
    source = 'Envía email a bruno@example.net: Dijo "HOLA": vuelve mañana.'
    content = json.dumps({"to": "bruno@example.net", "message": "HOLA", "type": "email"})
    with pytest.raises(ExtractionError, match="complete explicit body"):
        extract_notification(content, source)


@pytest.mark.parametrize("model_message", ["el técnico llega a las 16:45", "El técnico llega a las 16:45."])
def test_final_punctuation_and_case_come_from_the_request(model_message):
    source = "Manda un SMS al 611-999-000 diciendo que el técnico llega a las 16:45."
    content = json.dumps({"to": "611-999-000", "message": model_message, "type": "sms"})
    assert extract_notification(content, source).message == "el técnico llega a las 16:45."


def test_dropped_punctuation_is_not_restored_from_the_middle_of_a_request():
    source = "Envía email a juan@example.com diciendo hola. Gracias."
    content = json.dumps({"to": "juan@example.com", "message": "hola", "type": "email"})
    assert extract_notification(content, source).message == "hola"


@pytest.mark.parametrize("source", [
    "Envía email a ana@example.com y a juan@example.com: Hola.",
    "Manda un WhatsApp al 611222333: Hola.",
    "Envía SMS a ana@example.com: Hola.",
    "Envía email a juan..perez@example.com: Hola.",
    "Envía un email: Hola.",
])
def test_request_level_failures_are_classified_before_any_model_output(source):
    from app.guardrails import RequestRejected, check_request
    with pytest.raises(RequestRejected):
        check_request(source)
    # The same classification applies whatever the model returns.
    with pytest.raises(RequestRejected):
        extract_notification('{"to":"ana@example.com","message":"Hola.","type":"email"}', source)


def test_model_errors_are_not_classified_as_request_rejections():
    from app.guardrails import RequestRejected, check_request
    check_request(EMAIL_INPUT)
    with pytest.raises(ExtractionError) as info:
        extract_notification("REFUSED", EMAIL_INPUT)
    assert not isinstance(info.value, RequestRejected)
