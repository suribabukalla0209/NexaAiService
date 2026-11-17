import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import router
from app.core.config import Settings, get_settings
from app.core.logging_config import configure_logging
from app.repositories.message_repository import MessageRepository
from app.services.classification import MessageClassifier
from app.services.llm_provider import OpenAICompatibleProvider
from app.services.message_processing import MessageProcessor
from app.services.reply_generation import MessageAnalyzer, ReplyGenerator

logger = logging.getLogger(__name__)


async def _retention_loop(repository: MessageRepository, retention_days: int) -> None:
    while True:
        await asyncio.sleep(24 * 60 * 60)
        try:
            deleted = await asyncio.to_thread(
                repository.purge_expired_messages, retention_days
            )
            logger.info("Message retention cleanup completed: deleted=%s", deleted)
        except Exception as exc:
            logger.warning(
                "Message retention cleanup failed: error_type=%s", type(exc).__name__
            )


def create_app(
    settings: Settings | None = None,
    repository: MessageRepository | None = None,
    analyzer: MessageAnalyzer | None = None,
) -> FastAPI:
    configured = settings or get_settings()
    configure_logging()
    repo = repository or MessageRepository(configured.database_connection_url)
    if analyzer is None:
        provider = OpenAICompatibleProvider(configured)
        analyzer = MessageAnalyzer(MessageClassifier(provider), ReplyGenerator(provider))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        repo.initialize()
        try:
            deleted = await asyncio.to_thread(
                repo.purge_expired_messages, configured.message_retention_days
            )
            logger.info("Startup retention cleanup completed: deleted=%s", deleted)
        except Exception as exc:
            logger.warning(
                "Startup retention cleanup failed: error_type=%s", type(exc).__name__
            )
        retention_task = asyncio.create_task(
            _retention_loop(repo, configured.message_retention_days)
        )
        try:
            yield
        finally:
            retention_task.cancel()
            await asyncio.gather(retention_task, return_exceptions=True)

    application = FastAPI(
        title="Nexa AI WhatsApp Intelligence API",
        version="1.0.0",
        description=(
            "Environment-authenticated classification and suggested-reply API. "
            "The backend never sends WhatsApp replies."
        ),
        lifespan=lifespan,
    )

    @application.exception_handler(RequestValidationError)
    async def sanitized_validation_error(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        logger.info("Request validation rejected: error_count=%s", len(exc.errors()))
        return JSONResponse(
            status_code=422,
            content={"detail": "Invalid request payload"},
        )

    application.state.repository = repo
    application.state.processor = MessageProcessor(
        repo,
        analyzer,
        configured.processing_lease_seconds,
        configured.retry_max_attempts,
        configured.retry_base_delay_seconds,
        configured.retry_max_delay_seconds,
        configured.conversation_context_limit,
    )
    application.state.settings = configured
    application.dependency_overrides[get_settings] = lambda: configured

    application.include_router(router)
    return application


app = create_app()


#uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
#Set-Location ..
#uvicorn app.main:app --reload --host 127.0.0.1 --port 8000