import hashlib
import logging
from datetime import UTC, datetime, timedelta

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, Response, status
from jose import JWTError, jwt

from app.config import settings

logger = logging.getLogger(__name__)

ACCESS_COOKIE = "access_token"
REFRESH_COOKIE = "refresh_token"


def _fernet() -> Fernet:
    if not settings.token_encryption_key:
        raise RuntimeError("TOKEN_ENCRYPTION_KEY is not set")
    return Fernet(settings.token_encryption_key.encode("utf-8"))


def encrypt_github_token(token: str) -> str:
    return _fernet().encrypt(token.encode("utf-8")).decode("utf-8")


def decrypt_github_token(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("utf-8")).decode("utf-8")
    except InvalidToken as exc:
        logger.error("Failed to decrypt stored GitHub token")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stored credential could not be decrypted",
        ) from exc


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_access_jwt(user_id: str) -> str:
    expire = datetime.now(UTC) + timedelta(seconds=settings.access_token_ttl_seconds)
    return jwt.encode(
        {"sub": user_id, "exp": expire, "typ": "access"},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


def decode_access_jwt(token: str) -> str:
    try:
        payload = jwt.decode(
            token, settings.jwt_secret, algorithms=[settings.jwt_algorithm]
        )
    except JWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
        ) from exc
    user_id = payload.get("sub")
    if not user_id or payload.get("typ") != "access":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired session",
        )
    return user_id


def _cookie_kwargs(*, max_age: int, path: str = "/") -> dict:
    return {
        "httponly": True,
        "samesite": "lax",
        "secure": settings.cookie_secure,
        "max_age": max_age,
        "path": path,
    }


def set_auth_cookies(response: Response, *, access_jwt: str, refresh_token: str) -> None:
    response.set_cookie(
        ACCESS_COOKIE,
        access_jwt,
        **_cookie_kwargs(max_age=settings.access_token_ttl_seconds),
    )
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        **_cookie_kwargs(max_age=settings.refresh_token_ttl_seconds),
    )


def clear_auth_cookies(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/")
