import asyncio
import logging
from typing import Any

from app.repositories.message_repository import (
    DuplicateMessageConflict,
    MessageRepository,
    RetryCooldown,
    RetryLimitReached,
)
from app.schemas.messages import AnalysisResult, MessageResult, ProcessMessageRequest
from app.services.llm_provider import AIProviderError
from app.services.reply_generation import MessageAnalyzer

logger = logging.getLogger(__name__)


class ProcessingConflict(Exception):
    pass


class ProcessingLeaseLost(Exception):
    """The worker no longer owns the current processing claim."""


class MessageProcessor:
    def __init__(
        self,
        repository: MessageRepository,
        analyzer: MessageAnalyzer,
        lease_seconds: float = 180.0,
        retry_max_attempts: int = 3,
        retry_base_delay_seconds: float = 5.0,
        retry_max_delay_seconds: float = 300.0,
        conversation_context_limit: int = 10,
    ) -> None:
        self.repository = repository
        self.analyzer = analyzer
        self.lease_seconds = lease_seconds
        self.retry_max_attempts = retry_max_attempts
        self.retry_base_delay_seconds = retry_base_delay_seconds
        self.retry_max_delay_seconds = retry_max_delay_seconds
        self.conversation_context_limit = conversation_context_limit

    async def process(self, user_id: str, request: ProcessMessageRequest) -> MessageResult:
        try:
            state, record, claim_token = await asyncio.to_thread(
                self.repository.claim,
                user_id,
                request,
                self.lease_seconds,
                self.retry_max_attempts,
                self.conversation_context_limit,
            )
        except DuplicateMessageConflict as exc:
            raise ProcessingConflict(
                "message_id already exists with different message content"
            ) from exc

        if state != "CLAIMED":
            return MessageResult.model_validate(record)
        if claim_token is None:
            raise RuntimeError("Claimed message is missing its fencing token")

        try:
            saved_context = await asyncio.to_thread(
                self.repository.get_claim_context,
                user_id,
                request.message_id,
                request.conversation_id,
            )
            analysis_request = request.model_copy(
                update={"conversation_context": saved_context}
            )
            analysis = await self._analyze_with_lease(
                user_id, analysis_request, claim_token
            )
            saved: dict[str, Any] | None = await asyncio.to_thread(
                self.repository.complete,
                user_id,
                request.message_id,
                claim_token,
                analysis.model_dump(mode="json"),
                request.conversation_id,
            )
            if saved is not None:
                return MessageResult.model_validate(saved)

            # A newer claimant fenced this worker out. Return only the current stored state.
            current = await asyncio.to_thread(
                self.repository.get,
                user_id,
                request.message_id,
                request.conversation_id,
            )
            if current is None:
                raise ProcessingLeaseLost("Processing claim is no longer available")
            return MessageResult.model_validate(current)
        except AIProviderError as exc:
            cooldown = await asyncio.to_thread(
                self.repository.fail,
                user_id,
                request.message_id,
                claim_token,
                "AI provider unavailable or response invalid",
                retryable=exc.retryable,
                retry_after_seconds=exc.retry_after_seconds,
                retry_max_attempts=self.retry_max_attempts,
                retry_base_delay_seconds=self.retry_base_delay_seconds,
                retry_max_delay_seconds=self.retry_max_delay_seconds,
                conversation_id=request.conversation_id,
            )
            if cooldown and exc.retryable:
                exc.retry_after_seconds = max(
                    exc.retry_after_seconds or 0, cooldown
                )
            elif cooldown == 0:
                exc.retryable = False
                exc.retry_after_seconds = None
            logger.warning("Message analysis failed (provider error)")
            raise
        except ProcessingLeaseLost:
            # The worker must not transition a newer worker's lease to FAILED.
            raise
        except Exception as exc:
            await asyncio.to_thread(
                self.repository.fail,
                user_id,
                request.message_id,
                claim_token,
                "Message analysis failed",
                retryable=False,
                retry_after_seconds=None,
                retry_max_attempts=self.retry_max_attempts,
                retry_base_delay_seconds=self.retry_base_delay_seconds,
                retry_max_delay_seconds=self.retry_max_delay_seconds,
                conversation_id=request.conversation_id,
            )
            logger.error(
                "Message analysis failed (unexpected error): error_type=%s",
                type(exc).__name__,
            )
            raise

    async def _analyze_with_lease(
        self, user_id: str, request: ProcessMessageRequest, claim_token: str
    ) -> AnalysisResult:
        analysis_task = asyncio.create_task(self.analyzer.analyze(request))
        heartbeat_task = asyncio.create_task(
            self._maintain_lease(
                user_id,
                request.message_id,
                claim_token,
                request.conversation_id,
            )
        )
        try:
            done, _ = await asyncio.wait(
                (analysis_task, heartbeat_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat_task in done:
                error = heartbeat_task.exception()
                analysis_task.cancel()
                await asyncio.gather(analysis_task, return_exceptions=True)
                if error is not None:
                    raise error
                raise ProcessingLeaseLost("Processing lease renewal stopped")
            return analysis_task.result()
        finally:
            if not analysis_task.done():
                analysis_task.cancel()
            heartbeat_task.cancel()
            await asyncio.gather(
                analysis_task, heartbeat_task, return_exceptions=True
            )

    async def _maintain_lease(
        self,
        user_id: str,
        message_id: str,
        claim_token: str,
        conversation_id: str | None,
    ) -> None:
        interval = max(0.1, self.lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            renewed = await asyncio.to_thread(
                self.repository.renew_lease,
                user_id,
                message_id,
                claim_token,
                self.lease_seconds,
                conversation_id,
            )
            if not renewed:
                raise ProcessingLeaseLost("Processing claim was superseded or expired")
