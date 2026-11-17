import json
import logging
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import Settings

logger = logging.getLogger(__name__)


class AIProviderError(Exception):
    """Sanitized provider error metadata without the provider response body."""

    def __init__(
        self,
        message: str,
        *,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.status_code = status_code


class OpenAICompatibleProvider:
    def __init__(
        self,
        settings: Settings,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.settings = settings
        self.transport = transport

    async def generate(self, system_prompt: str, payload: dict[str, Any], schema: type[BaseModel]) -> BaseModel:
        api_key = self.settings.llm_api_key.get_secret_value()
        if not api_key:
            raise AIProviderError("LLM provider is not configured")
        body = {
            "model": self.settings.llm_model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__.lower(),
                    "strict": True,
                    "schema": schema.model_json_schema(),
                },
            },
        }
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.llm_timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post(
                    f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=body,
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
                if not isinstance(content, str):
                    raise AIProviderError("LLM returned an empty structured response")
                try:
                    return schema.model_validate_json(content)
                except ValidationError as exc:
                    raise AIProviderError("LLM returned an invalid structured response") from exc
        except AIProviderError:
            raise
        except httpx.HTTPStatusError as exc:
            # Status code is useful for setup diagnostics; never log headers or body.
            status_code = exc.response.status_code
            logger.warning(
                "LLM provider rejected request: status_code=%s",
                status_code,
            )
            raise AIProviderError(
                "LLM provider request failed",
                retryable=status_code == 429 or status_code >= 500,
                retry_after_seconds=self._parse_retry_after(
                    exc.response.headers.get("Retry-After")
                ),
                status_code=status_code,
            ) from exc
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            logger.warning("LLM provider transport failed: error_type=%s", type(exc).__name__)
            raise AIProviderError("LLM provider connection failed", retryable=True) from exc
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            # Never include provider response text, message payload, or API credentials.
            logger.warning(
                "LLM provider request failed: error_type=%s",
                type(exc).__name__,
            )
            raise AIProviderError("LLM provider request failed") from exc

    @staticmethod
    def _parse_retry_after(value: str | None) -> int | None:
        if not value:
            return None
        try:
            return max(0, int(float(value)))
        except ValueError:
            try:
                retry_at = parsedate_to_datetime(value)
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=UTC)
                return max(0, int((retry_at - datetime.now(UTC)).total_seconds()))
            except (TypeError, ValueError, OverflowError):
                return None
