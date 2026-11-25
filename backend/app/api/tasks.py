import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal, get_db
from app.deps import get_current_user
from app.models import Sandbox, Task, TaskEvent, User
from app.orchestrator.llm import LLMNotConfigured, get_chat_model
from app.orchestrator.runner import is_running, start_task_job
from app.orchestrator.store import load_events
from app.sandbox import get_sandbox_provider
from app.security import decrypt_github_token
from app.tools.runtime import ToolContext

router = APIRouter(prefix="/tasks", tags=["tasks"])


class CreateTaskBody(BaseModel):
    sandbox_id: str = Field(min_length=1)
    description: str = Field(min_length=1, max_length=8000)


class AnswerBody(BaseModel):
    answer: str = Field(min_length=1, max_length=8000)


def _task_out(task: Task, events: list[TaskEvent] | None = None) -> dict:
    body = {
        "task_id": task.id,
        "sandbox_id": task.sandbox_id,
        "description": task.description,
        "status": task.status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
    }
    if events is not None:
        body["events"] = [
            {
                "id": event.id,
                "event_type": event.event_type,
                "payload": event.payload,
                "timestamp": event.created_at.isoformat() if event.created_at else None,
            }
            for event in events
        ]
    return body


def _context(user: User, sandbox: Sandbox) -> ToolContext:
    return ToolContext(
        sandbox_id=sandbox.id,
        provider=get_sandbox_provider(),
        github_token=decrypt_github_token(user.github_access_token),
        repo_url=sandbox.repo_url,
        github_username=user.github_username,
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_task(
    body: CreateTaskBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_chat_model()
    except LLMNotConfigured as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="LLM is not configured. Set LLM_API_KEY or LLM_PROVIDER=fake.",
        ) from exc

    sandbox = await db.get(Sandbox, body.sandbox_id)
    if sandbox is None or sandbox.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sandbox not found")
    if sandbox.status != "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sandbox is not running",
        )

    task = Task(
        id=str(uuid.uuid4()),
        sandbox_id=sandbox.id,
        user_id=user.id,
        description=body.description.strip(),
        status="running",
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)

    start_task_job(
        task_id=task.id,
        description=task.description,
        ctx=_context(user, sandbox),
    )
    return {"task_id": task.id}


@router.get("")
async def list_tasks(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    rows = (
        await db.execute(
            select(Task, Sandbox.repo_url)
            .join(Sandbox, Sandbox.id == Task.sandbox_id)
            .where(Task.user_id == user.id)
            .order_by(Task.created_at.desc())
            .limit(50)
        )
    ).all()
    return {
        "tasks": [
            {
                "task_id": task.id,
                "sandbox_id": task.sandbox_id,
                "repo_url": repo_url,
                "description": task.description,
                "status": task.status,
                "created_at": task.created_at.isoformat() if task.created_at else None,
                "completed_at": task.completed_at.isoformat()
                if task.completed_at
                else None,
            }
            for task, repo_url in rows
        ]
    }


@router.get("/{task_id}")
async def get_task(
    task_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _owned_task(db, user, task_id)
    sandbox = await db.get(Sandbox, task.sandbox_id)
    events = (
        await db.execute(
            select(TaskEvent)
            .where(TaskEvent.task_id == task.id)
            .order_by(TaskEvent.id.asc())
        )
    ).scalars().all()
    body = _task_out(task, list(events))
    body["repo_url"] = sandbox.repo_url if sandbox else None
    return body


@router.post("/{task_id}/answer", status_code=status.HTTP_204_NO_CONTENT)
async def answer_task(
    task_id: str,
    body: AnswerBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    task = await _owned_task(db, user, task_id)
    if task.status != "waiting_for_user":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task is not waiting for a user answer",
        )
    if is_running(task.id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Task is already resuming",
        )
    sandbox = await db.get(Sandbox, task.sandbox_id)
    if sandbox is None or sandbox.status != "running":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Sandbox is not running",
        )
    saved = dict(task.agent_state or {})
    task.status = "running"
    await db.commit()
    start_task_job(
        task_id=task.id,
        description=task.description,
        ctx=_context(user, sandbox),
        saved_state=saved,
        resume_answer=body.answer.strip(),
    )
    return None


@router.get("/{task_id}/stream")
async def stream_task(
    request: Request,
    task_id: str,
):
    async with SessionLocal() as db:
        user = await get_current_user(request, db)
        await _owned_task(db, user, task_id)

    last_raw = request.headers.get("last-event-id") or "0"
    try:
        last_event_id = int(last_raw)
    except ValueError:
        last_event_id = 0

    async def events():
        nonlocal last_event_id
        idle = 0
        while True:
            rows = await load_events(task_id, last_event_id)
            for row in rows:
                last_event_id = row.id
                idle = 0
                payload = json.dumps(row.payload or {}, ensure_ascii=False)
                yield f"id: {row.id}\nevent: {row.event_type}\ndata: {payload}\n\n"
                if row.event_type == "done":
                    return
            async with SessionLocal() as db:
                current = await db.get(Task, task_id)
            if current is None:
                yield "event: done\ndata: {\"status\":\"failed\"}\n\n"
                return
            if current.status in {"done", "failed"} and not rows:
                yield f"event: done\ndata: {json.dumps({'status': current.status})}\n\n"
                return
            idle += 1
            if idle >= 30:
                idle = 0
                yield ": ping\n\n"
            await asyncio.sleep(0.5)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


async def _owned_task(db: AsyncSession, user: User, task_id: str) -> Task:
    task = await db.get(Task, task_id)
    if task is None or task.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task
