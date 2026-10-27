"""FastAPI entry point for the intelligent notification service."""

from contextlib import asynccontextmanager
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse
import httpx

from .config import Settings
from .models import CreateRequest, CreateResponse, RequestStatus, StatusResponse
from .provider import HTTPProvider
from .service import CapacityError, NotFoundError, Provider, RequestService


logging.basicConfig(level=logging.INFO)


def create_app(settings: Optional[Settings] = None, provider: Optional[Provider] = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        if provider is None:
            async with httpx.AsyncClient(
                base_url=settings.provider_base_url,
                headers={"X-API-Key": settings.provider_api_key},
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            ) as client:
                service = RequestService(HTTPProvider(client, settings), settings)
                application.state.service = service
                await service.start()
                try:
                    yield
                finally:
                    await service.stop()
        else:
            service = RequestService(provider, settings)
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

    @application.post("/v1/requests/{request_id}/process", response_model=StatusResponse)
    async def process_request(request_id: str) -> JSONResponse:
        try:
            current = await application.state.service.process(request_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Request not found") from exc
        except CapacityError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        # A completed request is an idempotent 200; active requests are accepted.
        status_code = 200 if current in (RequestStatus.sent, RequestStatus.failed) else 202
        return JSONResponse(
            status_code=status_code,
            content=StatusResponse(id=request_id, status=current).model_dump(mode="json"),
        )

    @application.get("/v1/requests/{request_id}", response_model=StatusResponse)
    async def get_request(request_id: str) -> StatusResponse:
        try:
            current = await application.state.service.get_status(request_id)
        except NotFoundError as exc:
            raise HTTPException(status_code=404, detail="Request not found") from exc
        return StatusResponse(id=request_id, status=current)

    return application


app = create_app()
