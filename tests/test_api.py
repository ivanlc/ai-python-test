import asyncio
from contextlib import asynccontextmanager
import time

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.providers import HTTPProvider, ProviderError


INPUT = "Enviar email a juan@example.com: hola"
OUTPUT = '{"to":"juan@example.com","message":"hola","type":"email"}'


class FakeProvider:
    def __init__(self, output=OUTPUT, error=None, gate=None):
        self.output = output
        self.error = error
        self.gate = gate
        self.extract_calls = 0
        self.notify_calls = []
        self.feedbacks = []

    async def extract(self, user_input, feedback=None):
        self.feedbacks.append(feedback)
        self.extract_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.output

    async def notify(self, notification, deadline=None):
        self.notify_calls.append(notification)


@asynccontextmanager
async def api_client(provider, **overrides):
    settings = Settings(**overrides)
    app = create_app(settings, provider)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                    base_url="http://test") as client:
            yield client, app.state.service


async def wait_for_status(client, request_id, expected):
    for _ in range(100):
        response = await client.get(f"/v1/requests/{request_id}")
        if response.json()["status"] == expected:
            return response
        await asyncio.sleep(0.005)
    pytest.fail(f"request never became {expected}")


async def test_contract_states_and_idempotency():
    gate = asyncio.Event()
    provider = FakeProvider(gate=gate)
    async with api_client(provider) as (client, _):
        created = await client.post("/v1/requests", json={"user_input": INPUT})
        assert created.status_code == 201
        request_id = created.json()["id"]
        assert (await client.get(f"/v1/requests/{request_id}")).json() == {
            "id": request_id, "status": "queued"}
        first = await client.post(f"/v1/requests/{request_id}/process")
        second = await client.post(f"/v1/requests/{request_id}/process")
        assert first.status_code == second.status_code == 202
        gate.set()
        assert (await wait_for_status(client, request_id, "sent")).status_code == 200
        assert (await client.post(f"/v1/requests/{request_id}/process")).status_code == 200
        assert provider.extract_calls == len(provider.notify_calls) == 1
        assert provider.notify_calls[0].type == "email"


async def test_invalid_input_and_unknown_id():
    async with api_client(FakeProvider()) as (client, _):
        assert (await client.post("/v1/requests", json={"user_input": ""})).status_code == 422
        assert (await client.post("/v1/requests", json={"user_input": "  \n "})).status_code == 422
        assert (await client.post("/v1/requests", json={})).status_code == 422
        assert (await client.get("/v1/requests/missing")).status_code == 404
        assert (await client.post("/v1/requests/missing/process")).status_code == 404


@pytest.mark.parametrize("output, re_asked", [
    # Unusable output (format): sampling noise, re-asked once.
    ("Refused: Content analysis flagged sensitive information.", True),
    ('Refused: cannot send. ' + OUTPUT, True),
    ('{"to":"juan@example.com","message":"wrong","message":"hola","type":"email"}', True),
    # Parsed but contradicts the request (content): failed without a re-ask.
    ('{"to":"other@example.com","message":"hola","type":"email"}', False),
    ('{"to":"juan@example.com","message":"ho","type":"email"}', False),
    # Protocol refusal: a decision, failed without a re-ask.
    ("REFUSED", False),
])
async def test_rejected_output_is_never_sent_and_only_format_errors_are_re_asked(output, re_asked):
    provider = FakeProvider(output=output)
    async with api_client(provider) as (client, _):
        request_id = (await client.post("/v1/requests", json={"user_input": INPUT})).json()["id"]
        await client.post(f"/v1/requests/{request_id}/process")
        await wait_for_status(client, request_id, "failed")
        assert provider.notify_calls == []
        assert (await client.post(f"/v1/requests/{request_id}/process")).status_code == 200
        assert provider.extract_calls == (2 if re_asked else 1)
        if re_asked:
            # The re-ask carries a fixed reason, never the model's text.
            assert provider.feedbacks[0] is None and provider.feedbacks[1]
            assert output not in provider.feedbacks[1] and "{" not in provider.feedbacks[1]


async def test_bounded_queue_and_worker_concurrency():
    gate = asyncio.Event()
    provider = FakeProvider(gate=gate)
    async with api_client(provider, worker_count=1, queue_capacity=1) as (client, _):
        ids = [(await client.post("/v1/requests", json={"user_input": INPUT})).json()["id"]
               for _ in range(3)]
        assert (await client.post(f"/v1/requests/{ids[0]}/process")).status_code == 202
        await wait_for_status(client, ids[0], "processing")
        assert (await client.post(f"/v1/requests/{ids[1]}/process")).status_code == 202
        assert (await client.post(f"/v1/requests/{ids[2]}/process")).status_code == 503
        assert (await client.get(f"/v1/requests/{ids[2]}")).json()["status"] == "queued"
        assert provider.extract_calls == 1
        gate.set()
        await wait_for_status(client, ids[1], "sent")
        assert (await client.post(f"/v1/requests/{ids[2]}/process")).status_code == 202
        await wait_for_status(client, ids[2], "sent")
        assert provider.extract_calls == 3


async def test_record_capacity():
    async with api_client(FakeProvider(), max_records=1) as (client, _):
        assert (await client.post("/v1/requests", json={"user_input": INPUT})).status_code == 201
        assert (await client.post("/v1/requests", json={"user_input": INPUT})).status_code == 503


async def test_expired_unstarted_request_can_be_pruned():
    async with api_client(FakeProvider(), max_records=1, record_ttl_seconds=1) as (client, service):
        old_id = (await client.post("/v1/requests", json={"user_input": INPUT})).json()["id"]
        service._records[old_id].updated_at = time.monotonic() - 2
        assert (await client.post("/v1/requests", json={"user_input": INPUT})).status_code == 201
        assert (await client.get(f"/v1/requests/{old_id}")).status_code == 404


async def test_concurrent_process_calls_schedule_one_job():
    gate = asyncio.Event()
    provider = FakeProvider(gate=gate)
    async with api_client(provider, worker_count=2) as (client, _):
        request_id = (await client.post("/v1/requests", json={"user_input": INPUT})).json()["id"]
        responses = await asyncio.gather(*(
            client.post(f"/v1/requests/{request_id}/process") for _ in range(100)))
        assert all(response.status_code == 202 for response in responses)
        await wait_for_status(client, request_id, "processing")
        assert provider.extract_calls == 1
        gate.set()
        await wait_for_status(client, request_id, "sent")
        assert len(provider.notify_calls) == 1


async def test_worker_limit_holds_with_multiple_queued_jobs():
    gate = asyncio.Event()
    provider = FakeProvider(gate=gate)
    async with api_client(provider, worker_count=2, queue_capacity=10) as (client, _):
        ids = [(await client.post("/v1/requests", json={"user_input": INPUT})).json()["id"]
               for _ in range(6)]
        for request_id in ids:
            await client.post(f"/v1/requests/{request_id}/process")
        await wait_for_status(client, ids[1], "processing")
        assert provider.extract_calls == 2
        assert (await client.get(f"/v1/requests/{ids[2]}")).json()["status"] == "queued"
        gate.set()
        for request_id in ids:
            await wait_for_status(client, request_id, "sent")


async def test_provider_failure_is_terminal_without_delivery():
    provider = FakeProvider(error=ProviderError("timeout"))
    async with api_client(provider) as (client, _):
        request_id = (await client.post("/v1/requests", json={"user_input": INPUT})).json()["id"]
        await client.post(f"/v1/requests/{request_id}/process")
        await wait_for_status(client, request_id, "failed")
        await client.post(f"/v1/requests/{request_id}/process")
        assert provider.extract_calls == 1
        assert provider.notify_calls == []


def test_openapi_documents_both_process_success_codes():
    responses = create_app().openapi()["paths"]["/v1/requests/{request_id}/process"]["post"]["responses"]
    assert {"200", "202"} <= responses.keys()


async def test_api_pipeline_with_http_adapter():
    paths = []

    def handler(request):
        paths.append(request.url.path)
        assert request.headers["X-API-Key"] == "test-dev-2026"
        if request.url.path == "/v1/ai/extract":
            return httpx.Response(200, json={"choices": [{"message": {"content": OUTPUT}}]})
        return httpx.Response(200, json={"status": "delivered", "provider_id": "p-1"})

    settings = Settings()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                base_url=settings.provider_base_url,
                                headers={"X-API-Key": settings.provider_api_key}) as external_client:
        async with api_client(HTTPProvider(external_client, settings)) as (client, _):
            request_id = (await client.post("/v1/requests", json={"user_input": INPUT})).json()["id"]
            await client.post(f"/v1/requests/{request_id}/process")
            await wait_for_status(client, request_id, "sent")
    assert paths == ["/v1/ai/extract", "/v1/notify"]


@pytest.mark.parametrize("failure_stage", ["extract", "notify"])
async def test_unexpected_job_error_does_not_kill_worker(failure_stage, caplog):
    class FaultOnceProvider(FakeProvider):
        notify_attempts = 0

        async def extract(self, user_input, feedback=None):
            if failure_stage == "extract" and self.extract_calls == 0:
                self.extract_calls += 1
                raise RuntimeError("unexpected extraction failure")
            return await super().extract(user_input)

        async def notify(self, notification, deadline=None):
            self.notify_attempts += 1
            if failure_stage == "notify" and self.notify_attempts == 1:
                raise RuntimeError("unexpected notification failure")
            await super().notify(notification)

    provider = FaultOnceProvider()
    async with api_client(provider, worker_count=1) as (client, _):
        ids = [(await client.post("/v1/requests", json={"user_input": INPUT})).json()["id"]
               for _ in range(2)]
        for request_id in ids:
            await client.post(f"/v1/requests/{request_id}/process")
        await wait_for_status(client, ids[0], "failed")
        await wait_for_status(client, ids[1], "sent")
        await client.post(f"/v1/requests/{ids[0]}/process")
        assert provider.extract_calls == 2
        assert provider.notify_attempts == (1 if failure_stage == "extract" else 2)
        assert len(provider.notify_calls) == 1
        assert any(record.exc_info for record in caplog.records
                   if "Unexpected processing failure" in record.getMessage())


async def test_shutdown_cancels_inflight_job_without_sending():
    provider = FakeProvider(gate=asyncio.Event())

    async def start_job_and_exit():
        async with api_client(provider, worker_count=1) as (client, _):
            request_id = (await client.post("/v1/requests", json={"user_input": INPUT})).json()["id"]
            await client.post(f"/v1/requests/{request_id}/process")
            await wait_for_status(client, request_id, "processing")
        assert provider.notify_calls == []

    await asyncio.wait_for(start_job_and_exit(), timeout=1)


async def test_bookkeeping_error_does_not_shrink_the_worker_pool():
    provider = FakeProvider()
    async with api_client(provider, worker_count=1) as (client, service):
        # Simulate an inconsistent queue entry; the single worker must survive it.
        await service._queue.put("missing-id")
        created = await client.post("/v1/requests", json={"user_input": INPUT})
        request_id = created.json()["id"]
        await client.post(f"/v1/requests/{request_id}/process")
        await wait_for_status(client, request_id, "sent")
        assert all(not worker.done() for worker in service._workers)


class SequenceProvider(FakeProvider):
    def __init__(self, outputs):
        super().__init__()
        self.outputs = list(outputs)

    async def extract(self, user_input, feedback=None):
        await super().extract(user_input, feedback)
        return self.outputs.pop(0)


async def run_one(provider, user_input=INPUT, **settings):
    async with api_client(provider, **settings) as (client, _):
        request_id = (await client.post("/v1/requests", json={"user_input": user_input})).json()["id"]
        await client.post(f"/v1/requests/{request_id}/process")
        for _ in range(200):
            status = (await client.get(f"/v1/requests/{request_id}")).json()["status"]
            if status in ("sent", "failed"):
                return status
            await asyncio.sleep(0.005)
    pytest.fail("job did not finish")


async def test_invalid_model_output_is_re_asked_once_and_can_recover():
    provider = SequenceProvider(["Lo siento, no puedo.", OUTPUT])
    assert await run_one(provider) == "sent"
    assert provider.feedbacks == [None, "Model refused the request"]
    assert len(provider.notify_calls) == 1


async def test_re_ask_budget_is_configurable():
    provider = SequenceProvider(["REFUSED"])
    assert await run_one(provider, extraction_retries=0) == "failed"
    assert provider.extract_calls == 1


@pytest.mark.parametrize("user_input", [
    "Envía email a ana@example.com y a juan@example.com: Hola.",
    "Manda un WhatsApp al 611222333: Hola.",
    "Envía SMS a ana@example.com: Hola.",
    "Envía email a juan..perez@example.com: Hola.",
])
async def test_request_level_rejection_never_calls_the_model(user_input):
    provider = FakeProvider()
    assert await run_one(provider, user_input) == "failed"
    assert provider.extract_calls == 0 and provider.notify_calls == []


async def test_job_deadline_bounds_a_hanging_extraction():
    provider = FakeProvider(gate=asyncio.Event())  # never released
    assert await run_one(provider, job_timeout_seconds=0.05) == "failed"
    assert provider.notify_calls == []


async def test_notify_receives_the_job_deadline():
    class DeadlineProvider(FakeProvider):
        async def notify(self, notification, deadline=None):
            self.deadline = deadline
            await super().notify(notification)

    provider = DeadlineProvider()
    before = time.monotonic()
    assert await run_one(provider, job_timeout_seconds=30) == "sent"
    assert before + 29 < provider.deadline <= time.monotonic() + 30


async def test_protocol_refusal_is_a_decision_and_is_not_re_asked():
    provider = SequenceProvider(["REFUSED"])
    assert await run_one(provider) == "failed"
    assert provider.extract_calls == 1
