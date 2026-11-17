import asyncio
import logging
import math
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.core.auth import authenticated_user, processing_user
from app.core.config import Settings, get_settings
from app.repositories.message_repository import (
    MessageRepository,
    RetryCooldown,
    RetryLimitReached,
)
from app.schemas.messages import (
    BatchProcessRequest,
    BatchProcessResponse,
    MessageListResponse,
    MessageResult,
    ProcessMessageRequest,
    ProcessingStatus,
)
from app.services.llm_provider import AIProviderError
from app.services.message_processing import (
    MessageProcessor,
    ProcessingConflict,
    ProcessingLeaseLost,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1")
UserId = Annotated[str, Depends(authenticated_user)]
# Optional bearer identity for processing; no Authorization header uses the local-only scope.
ProcessingUserId = Annotated[str, Depends(processing_user)]


def _repository(request: Request) -> MessageRepository:
    return request.app.state.repository


def _processor(request: Request) -> MessageProcessor:
    return request.app.state.processor


async def _enforce_rate_limit(request: Request, user_id: str, cost: int = 1) -> None:
    settings: Settings = request.app.state.settings
    retry_after = await asyncio.to_thread(
        request.app.state.repository.consume_rate_limit,
        user_id,
        settings.rate_limit_requests,
        settings.rate_limit_window_seconds,
        cost,
    )
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="Rate limit exceeded; retry after the current window",
            headers={"Retry-After": str(retry_after)},
        )


def _provider_failure(exc: AIProviderError, settings: Settings) -> HTTPException:
    retryable = exc.retryable
    status_code = 429 if exc.status_code == 429 else 503 if retryable else 502
    headers = None
    if retryable:
        retry_after = max(
            exc.retry_after_seconds or 0,
            math.ceil(settings.retry_base_delay_seconds),
        )
        headers = {"Retry-After": str(retry_after)}
    return HTTPException(
        status_code=status_code,
        detail="AI provider request failed; retry according to Retry-After when supplied",
        headers=headers,
    )


@router.get("/health", tags=["health"])
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready", tags=["health"])
async def readiness(request: Request) -> dict[str, str]:
    try:
        await asyncio.to_thread(request.app.state.repository.check_ready)
    except Exception as exc:
        logger.warning("Database readiness failed: error_type=%s", type(exc).__name__)
        raise HTTPException(status_code=503, detail="Service is not ready") from exc
    return {"status": "ready"}


@router.post("/messages/process", response_model=MessageResult, tags=["messages"])
async def process_message(
    payload: ProcessMessageRequest,
    user_id: ProcessingUserId,
    request: Request,
    processor: Annotated[MessageProcessor, Depends(_processor)],
) -> MessageResult:
    await _enforce_rate_limit(request, user_id)
    try:
        return await processor.process(user_id, payload)
    except ProcessingConflict as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ProcessingLeaseLost as exc:
        raise HTTPException(status_code=409, detail="Processing lease was lost; retry the request") from exc
    except RetryCooldown as exc:
        raise HTTPException(
            status_code=429,
            detail="Message retry is cooling down",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except RetryLimitReached as exc:
        raise HTTPException(status_code=409, detail="Message retry limit reached") from exc
    except AIProviderError as exc:
        raise _provider_failure(exc, request.app.state.settings) from exc
    except Exception as exc:
        logger.error(
            "Message processing request failed: error_type=%s", type(exc).__name__
        )
        raise HTTPException(status_code=500, detail="Message processing failed") from exc


@router.post("/messages/process/batch", response_model=BatchProcessResponse, tags=["messages"])
async def process_batch(
    payload: BatchProcessRequest,
    user_id: ProcessingUserId,
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    processor: Annotated[MessageProcessor, Depends(_processor)],
) -> BatchProcessResponse:
    if len(payload.messages) > settings.batch_size_limit:
        raise HTTPException(status_code=413, detail="Batch size exceeds configured limit")
    await _enforce_rate_limit(request, user_id, cost=len(payload.messages))
    results: list[MessageResult] = []
    for message in payload.messages:
        try:
            results.append(await processor.process(user_id, message))
        except ProcessingConflict as exc:
            results.append(
                MessageResult(
                    message_id=message.message_id,
                    conversation_id=message.conversation_id,
                    sender_name=message.sender_name,
                    original_message=message.original_message,
                    processing_status=ProcessingStatus.FAILED,
                    processing_error=str(exc),
                    received_at=message.received_at,
                )
            )
        except Exception:
            record = await asyncio.to_thread(
                processor.repository.get,
                user_id,
                message.message_id,
                message.conversation_id,
            )
            if record is None:
                raise HTTPException(status_code=500, detail="Batch message processing failed")
            results.append(MessageResult.model_validate(record))
    return BatchProcessResponse(results=results)


@router.get("/messages", response_model=MessageListResponse, tags=["messages"])
async def list_messages(
    user_id: UserId,
    request: Request,
    repository: Annotated[MessageRepository, Depends(_repository)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> MessageListResponse:
    await _enforce_rate_limit(request, user_id)
    items, total = await asyncio.to_thread(repository.list, user_id, limit, offset)
    return MessageListResponse(items=items, limit=limit, offset=offset, total=total)


@router.get("/messages/{message_id}", response_model=MessageResult, tags=["messages"])
async def get_message(
    message_id: str,
    user_id: UserId,
    request: Request,
    repository: Annotated[MessageRepository, Depends(_repository)],
    conversation_id: Annotated[
        str | None,
        Query(min_length=1, max_length=200, pattern=r"^[^\s]+$"),
    ] = None,
) -> MessageResult:
    await _enforce_rate_limit(request, user_id)
    record = await asyncio.to_thread(
        repository.get, user_id, message_id, conversation_id
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Message not found")
    return MessageResult.model_validate(record)


@router.post("/messages/{message_id}/retry", response_model=MessageResult, tags=["messages"])
async def retry_message(
    message_id: str,
    user_id: UserId,
    request: Request,
    repository: Annotated[MessageRepository, Depends(_repository)],
    processor: Annotated[MessageProcessor, Depends(_processor)],
    conversation_id: Annotated[
        str | None,
        Query(min_length=1, max_length=200, pattern=r"^[^\s]+$"),
    ] = None,
) -> MessageResult:
    await _enforce_rate_limit(request, user_id)
    payload = await asyncio.to_thread(
        repository.get_for_retry, user_id, message_id, conversation_id
    )
    if payload is None:
        raise HTTPException(status_code=404, detail="Failed message not found")
    try:
        return await processor.process(user_id, payload)
    except ProcessingConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ProcessingLeaseLost as exc:
        raise HTTPException(status_code=409, detail="Processing lease was lost; retry later") from exc
    except RetryCooldown as exc:
        raise HTTPException(
            status_code=429,
            detail="Message retry is cooling down",
            headers={"Retry-After": str(exc.retry_after_seconds)},
        ) from exc
    except RetryLimitReached as exc:
        raise HTTPException(status_code=409, detail="Message retry limit reached") from exc
    except AIProviderError as exc:
        raise _provider_failure(exc, request.app.state.settings) from exc
