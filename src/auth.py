from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import settings

_bearer = HTTPBearer()


def require_auth(
    credentials: HTTPAuthorizationCredentials = Security(_bearer),
) -> dict:
    if credentials.credentials != settings.api_key:
        raise HTTPException(
            status_code=401,
            detail={
                "error": {
                    "code": "authentication_required",
                    "message": "Invalid or missing API key.",
                    "request_id": "",
                }
            },
        )
    return {"tenant_id": settings.tenant_id, "user_id": "user_dev_001"}
