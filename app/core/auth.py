import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings

_bearer = HTTPBearer(auto_error=False)


def authenticated_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    return _resolve_authenticated_user(credentials, settings)



def processing_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> str:
    authorization = request.headers.get("authorization")
    if authorization is None:
        if settings.environment == "development":
            return "local-development"
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    credentials = HTTPAuthorizationCredentials(
        scheme=parts[0],
        credentials=parts[1],
    )
    return _resolve_authenticated_user(credentials, settings)



def _resolve_authenticated_user(
    credentials: HTTPAuthorizationCredentials | None, settings: Settings
) -> str:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Bearer authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Compare against every configured token to avoid returning an early match.
    matched_user: str | None = None
    candidate = credentials.credentials
    for configured_token, user_id in settings.access_tokens_json.items():
        if hmac.compare_digest(candidate, configured_token):
            matched_user = user_id

    if matched_user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return matched_user
