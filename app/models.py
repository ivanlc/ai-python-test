"""Public API models and the notification domain model."""

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RequestStatus(str, Enum):
    queued = "queued"
    processing = "processing"
    sent = "sent"
    failed = "failed"


class CreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_input: str = Field(min_length=1, max_length=4000)


class CreateResponse(BaseModel):
    id: str


class StatusResponse(BaseModel):
    id: str
    status: RequestStatus


class Notification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to: str
    message: str
    type: Literal["email", "sms"]
