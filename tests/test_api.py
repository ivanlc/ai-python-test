import asyncio
from contextlib import asynccontextmanager
import time

import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.provider import HTTPProvider, ProviderError


INPUT = "Enviar email a juan@example.com: hola"
OUTPUT = '{"to":"juan@example.com","message":"hola","type":"email"}'


class FakeProvider:
    def __init__(self, output=OUTPUT, error=None, gate=None):
        self.output = output
        self.error = error
        self.gate = gate
        self.extract_calls = 0
        self.notify_calls = []

    async def extract(self, user_input):
        self.extract_calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.error is not None:
            raise self.error
        return self.output

    async def notify(self, notification):
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


async def test_refusal_and_invalid_recipient_do_not_notify():
    for output in ("Refused: Content analysis flagged sensitive information.",
                   '{"to":"other@example.com","message":"hola","type":"email"}',
                   'Refused: cannot send. ' + OUTPUT,
                   '{"to":"juan@example.com","message":"wrong","message":"hola","type":"email"}'):
        provider = FakeProvider(output=output)
        async with api_client(provider) as (client, _):
            request_id = (await client.post("/v1/requests", json={"user_input": INPUT})).json()["id"]
            await client.post(f"/v1/requests/{request_id}/process")
            await wait_for_status(client, request_id, "failed")
            assert provider.notify_calls == []
            assert (await client.post(f"/v1/requests/{request_id}/process")).status_code == 200
            assert provider.extract_calls == 1


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
