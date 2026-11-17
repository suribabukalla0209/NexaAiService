from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Classification(StrEnum):
    IMPORTANT = "IMPORTANT"
    NORMAL = "NORMAL"


class ProcessingStatus(StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ContextMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender_name: Annotated[str, Field(min_length=1, max_length=120)]
    text: Annotated[str, Field(max_length=4000)]


class ProcessMessageRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[^\s]+$")]
    conversation_id: Annotated[
        str, Field(min_length=1, max_length=200, pattern=r"^[^\s]+$")
    ] | None = None
    sender_name: Annotated[str, Field(min_length=1, max_length=120)]
    original_message: Annotated[str, Field(min_length=1, max_length=5000)]
    received_at: datetime | None = None
    conversation_context: Annotated[list[ContextMessage], Field(max_length=10)] = Field(
        default_factory=list
    )
    response_language: Annotated[str, Field(min_length=2, max_length=40)] = "same as message"

    @field_validator("received_at")
    @classmethod
    def normalize_received_at(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("received_at must include a timezone offset")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def validate_conversation_context_scope(self) -> "ProcessMessageRequest":
        if self.conversation_id is not None and self.conversation_context:
            raise ValueError(
                "conversation_context must be omitted when conversation_id is supplied"
            )
        return self


class AnalysisResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: Classification
    priority_score: Annotated[float, Field(ge=0.0, le=1.0)]
    explanation: Annotated[str, Field(min_length=1, max_length=500)]
    detected_language: Annotated[str, Field(min_length=1, max_length=80)] = "unknown"
    requires_response: bool
    suggested_response: Annotated[str, Field(max_length=1000)] = ""
    ai_response_generated_at: datetime | None = None


class MessageResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message_id: str
    conversation_id: str | None = None
    sender_name: str
    original_message: str
    classification: Classification | None = None
    priority_score: float | None = None
    explanation: str | None = None
    detected_language: str | None = None
    requires_response: bool | None = None
    suggested_response: str = ""
    processing_status: ProcessingStatus
    processing_error: str | None = None
    received_at: datetime | None = None
    backend_received_at: datetime | None = None
    processing_started_at: datetime | None = None
    ai_response_generated_at: datetime | None = None
    processing_duration_ms: int | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    processed_at: datetime | None = None


class BatchProcessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: Annotated[list[ProcessMessageRequest], Field(min_length=1, max_length=100)]


class BatchProcessResponse(BaseModel):
    results: list[MessageResult]


class MessageListResponse(BaseModel):
    items: list[MessageResult]
    limit: int
    offset: int
    total: int
