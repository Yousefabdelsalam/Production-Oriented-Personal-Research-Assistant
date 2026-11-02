from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.container import Services


def get_services(request: Request) -> Services:
    return request.app.state.services


def require_token(request: Request, authorization: str | None = Header(default=None)) -> None:
    token = request.app.state.services.settings.app_api_token
    if not token:
        return
    supplied = (authorization or "").removeprefix("Bearer ").strip()
    if not hmac.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")


def get_db(services: Services = Depends(get_services)):
    s = services.session_factory()
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


DB = Depends(get_db)
