"""FastAPI entry point for the intelligent notification service."""

from contextlib import AsyncExitStack, asynccontextmanager
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Response, status
import httpx

from .config import Settings
from .models import CreateRequest, CreateResponse, RequestStatus, StatusResponse
from .providers import HTTPProvider
from .pipeline import CapacityError, NotFoundError, Provider, RequestService


logging.basicConfig(level=logging.INFO)


def create_app(settings: Optional[Settings] = None, provider: Optional[Provider] = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        async with AsyncExitStack() as stack:
            active_provider = provider
            if active_provider is None:
                client = await stack.enter_async_context(httpx.AsyncClient(
                    base_url=settings.provider_base_url,
                    headers={"X-API-Key": settings.provider_api_key},
                    limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
                ))
                active_provider = HTTPProvider(client, settings)
            service = RequestService(active_provider, settings)
            application.state.service = service
            await service.start()
            try:
                yield
            finally:
                await service.stop()

    application = FastAPI(title="Intelligent Notification Service", lifespan=lifespan)

    @application.post("/v1/requests", status_code=status.HTTP_201_CREATED,
                      response_model=CreateResponse)
    async def create_request(body: CreateRequest) -> CreateResponse:
        try:
            request_id = await application.state.service.create(body.user_input)
        except CapacityError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        return CreateResponse(id=request_id)

    @application.post("/v1/requests/{request_id}/process", status_code=status.HTTP_202_ACCEPTED,
                      response_model=StatusResponse, responses={200: {"model": StatusResponse}})
    async def process_request(request_id: str, response: Response) -> StatusResponse:
        try:
            current = await application.state.service.process(request_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Request not found") from exc
        except CapacityError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # A completed request is an idempotent 200; active requests are accepted.
        response.status_code = 200 if current in (RequestStatus.sent, RequestStatus.failed) else 202
        return StatusResponse(id=request_id, status=current)

    @application.get("/v1/requests/{request_id}", response_model=StatusResponse)
    async def get_request(request_id: str) -> StatusResponse:
        try:
            current = await application.state.service.get_status(request_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Request not found") from exc
        return StatusResponse(id=request_id, status=current)

    return application


app = create_app()
