import asyncio
import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.github import (
    REAUTH_DETAIL,
    authenticated_clone_url,
    public_github_url,
    user_has_repo_access,
)
from app.models import Sandbox, User
from app.sandbox import (
    GitAuthError,
    SandboxError,
    SandboxNotFound,
    ShellNotFound,
    get_sandbox_provider,
)
from app.security import decrypt_github_token
from app.sandbox.docker_provider import DockerSandboxProvider
from app.sandbox.provider import SandboxProvider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])


class CreateSandboxBody(BaseModel):
    repo_full_name: str = Field(min_length=3, max_length=200, pattern=r"^[^/\s]+/[^/\s]+$")


class ExecBody(BaseModel):
    command: str = Field(min_length=1)


class ShellCommandBody(BaseModel):
    command: str = Field(min_length=1)
    shell_session_id: str | None = None


def _provider() -> SandboxProvider:
    return get_sandbox_provider()


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_sandbox(
    body: CreateSandboxBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    provider: SandboxProvider = Depends(_provider),
):
    full_name = body.repo_full_name.strip()
    token = decrypt_github_token(user.github_access_token)
    if not await user_has_repo_access(token, full_name):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Repository is not accessible with this GitHub account",
        )

    clone_url = authenticated_clone_url(full_name, token)
    stored_url = public_github_url(full_name)
    limits = {
        "cpus": settings.sandbox_cpus,
        "memory": settings.sandbox_memory,
    }
    try:
        sandbox_id = await asyncio.to_thread(
            provider.create_sandbox, clone_url, limits
        )
    except GitAuthError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=REAUTH_DETAIL,
        )
    except SandboxError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc) or "Sandbox create failed",
        ) from exc

    container_id = None
    if isinstance(provider, DockerSandboxProvider):
        container_id = await asyncio.to_thread(
            provider.docker_container_id, sandbox_id
        )

    row = Sandbox(
        id=sandbox_id,
        user_id=user.id,
        repo_url=stored_url,
        container_id=container_id,
        status="running",
    )
    db.add(row)
    try:
        await db.commit()
    except Exception:
        await asyncio.to_thread(provider.destroy, sandbox_id)
        raise
    return {"sandbox_id": sandbox_id}


@router.post("/{sandbox_id}/exec")
async def exec_sandbox(
    sandbox_id: str,
    body: ExecBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    provider: SandboxProvider = Depends(_provider),
):
    row = await _owned_sandbox(db, user, sandbox_id)
    if row.status == "destroyed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sandbox already destroyed",
        )
    try:
        return await asyncio.to_thread(provider.exec, sandbox_id, body.command)
    except SandboxNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sandbox is not running",
        ) from exc
    except SandboxError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc) or "Sandbox exec failed",
        ) from exc


@router.post("/{sandbox_id}/shell", status_code=status.HTTP_201_CREATED)
async def open_shell(
    sandbox_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    provider: SandboxProvider = Depends(_provider),
):
    await _require_running(db, user, sandbox_id)
    try:
        shell_session_id = await asyncio.to_thread(provider.open_shell, sandbox_id)
    except SandboxNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sandbox is not running",
        ) from exc
    except SandboxError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc) or "Failed to open shell",
        ) from exc
    return {"shell_session_id": shell_session_id}


@router.post("/{sandbox_id}/shell/command", status_code=status.HTTP_204_NO_CONTENT)
async def send_shell_command(
    sandbox_id: str,
    body: ShellCommandBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    provider: SandboxProvider = Depends(_provider),
):
    await _require_running(db, user, sandbox_id)
    session_id = body.shell_session_id
    if session_id is None:
        session_id = _latest_shell_id(provider, sandbox_id)
    try:
        await asyncio.to_thread(provider.send_command, session_id, body.command)
    except ShellNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Shell session is not open",
        ) from exc
    except SandboxError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc) or "Failed to send command",
        ) from exc
    return None


@router.get("/{sandbox_id}/shell/stream")
async def stream_shell(
    request: Request,
    sandbox_id: str,
    shell_session_id: str | None = None,
    provider: SandboxProvider = Depends(_provider),
):
    async with SessionLocal() as db:
        user = await get_current_user(request, db)
        await _require_running(db, user, sandbox_id)

    session_id = shell_session_id
    if session_id is None:
        session_id = _latest_shell_id(provider, sandbox_id)

    last_raw = request.headers.get("last-event-id") or "0"
    try:
        last_event_id = int(last_raw)
    except ValueError:
        last_event_id = 0

    async def events():
        agen = provider.stream_output(session_id, last_event_id)
        try:
            while True:
                try:
                    event_id, chunk = await asyncio.wait_for(anext(agen), timeout=15)
                except StopAsyncIteration:
                    yield "event: closed\ndata: {}\n\n"
                    break
                except TimeoutError:
                    yield ": ping\n\n"
                    continue
                payload = json.dumps({"text": chunk}, ensure_ascii=False)
                yield f"id: {event_id}\nevent: output\ndata: {payload}\n\n"
        except ShellNotFound:
            yield "event: closed\ndata: {}\n\n"
        finally:
            await agen.aclose()

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/{sandbox_id}", status_code=status.HTTP_204_NO_CONTENT)
async def destroy_sandbox(
    sandbox_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    provider: SandboxProvider = Depends(_provider),
):
    row = await _owned_sandbox(db, user, sandbox_id)
    try:
        await asyncio.to_thread(provider.destroy, sandbox_id)
    except SandboxError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc) or "Sandbox destroy failed",
        ) from exc
    row.status = "destroyed"
    row.destroyed_at = datetime.now(UTC)
    await db.commit()
    return None


async def _owned_sandbox(db: AsyncSession, user: User, sandbox_id: str) -> Sandbox:
    row = await db.get(Sandbox, sandbox_id)
    if row is None or row.user_id != user.id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Sandbox not found",
        )
    return row


async def _require_running(db: AsyncSession, user: User, sandbox_id: str) -> Sandbox:
    row = await _owned_sandbox(db, user, sandbox_id)
    if row.status == "destroyed":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sandbox already destroyed",
        )
    return row


def _latest_shell_id(provider: SandboxProvider, sandbox_id: str) -> str:
    latest = getattr(provider, "latest_shell_id", None)
    if latest is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="shell_session_id is required",
        )
    try:
        return latest(sandbox_id)
    except ShellNotFound as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No open shell session",
        ) from exc
