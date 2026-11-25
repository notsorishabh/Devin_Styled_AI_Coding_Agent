import logging
import secrets
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_db
from app.models import User
from app.oauth import oauth
from app.security import (
    REFRESH_COOKIE,
    clear_auth_cookies,
    create_access_jwt,
    encrypt_github_token,
    hash_refresh_token,
    set_auth_cookies,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])


def _frontend_redirect(error: str | None = None) -> RedirectResponse:
    url = settings.frontend_origin.rstrip("/")
    if error:
        return RedirectResponse(url=f"{url}/?error={error}", status_code=302)
    return RedirectResponse(url=f"{url}/", status_code=302)


@router.get("/github/login")
async def github_login(request: Request):
    if not settings.github_client_id or not settings.github_client_secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="GitHub OAuth is not configured",
        )
    return await oauth.github.authorize_redirect(
        request, settings.github_oauth_redirect_uri
    )


@router.get("/github/callback")
async def github_callback(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        token = await oauth.github.authorize_access_token(request)
    except Exception:
        logger.exception("GitHub OAuth token exchange failed")
        return _frontend_redirect("oauth_failed")

    access_token = token.get("access_token")
    if not access_token:
        return _frontend_redirect("oauth_failed")

    resp = await oauth.github.get("user", token=token)
    if resp.status_code >= 400:
        logger.error("GitHub /user failed with status %s", resp.status_code)
        return _frontend_redirect("oauth_failed")
    profile = resp.json()
    github_id = profile.get("id")
    github_username = profile.get("login")
    if github_id is None or not github_username:
        return _frontend_redirect("oauth_failed")

    result = await db.execute(select(User).where(User.github_id == github_id))
    user = result.scalar_one_or_none()
    refresh_raw = secrets.token_urlsafe(32)
    encrypted = encrypt_github_token(access_token)
    refresh_hash = hash_refresh_token(refresh_raw)

    if user is None:
        user = User(
            id=str(uuid.uuid4()),
            github_id=github_id,
            github_username=github_username,
            github_access_token=encrypted,
            refresh_token=refresh_hash,
        )
        db.add(user)
    else:
        user.github_username = github_username
        user.github_access_token = encrypted
        user.refresh_token = refresh_hash

    await db.commit()

    response = _frontend_redirect()
    set_auth_cookies(
        response,
        access_jwt=create_access_jwt(user.id),
        refresh_token=refresh_raw,
    )
    return response


@router.post("/refresh")
async def refresh_session(request: Request, db: AsyncSession = Depends(get_db)):
    raw = request.cookies.get(REFRESH_COOKIE)
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )
    token_hash = hash_refresh_token(raw)
    result = await db.execute(select(User).where(User.refresh_token == token_hash))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    new_refresh = secrets.token_urlsafe(32)
    user.refresh_token = hash_refresh_token(new_refresh)
    await db.commit()

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    set_auth_cookies(
        response,
        access_jwt=create_access_jwt(user.id),
        refresh_token=new_refresh,
    )
    return response


@router.post("/logout")
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    raw = request.cookies.get(REFRESH_COOKIE)
    if raw:
        token_hash = hash_refresh_token(raw)
        result = await db.execute(select(User).where(User.refresh_token == token_hash))
        user = result.scalar_one_or_none()
        if user is not None:
            user.refresh_token = None
            await db.commit()

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    clear_auth_cookies(response)
    return response
