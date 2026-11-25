from __future__ import annotations

from datetime import UTC, datetime

from langchain_core.messages import messages_from_dict, messages_to_dict
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Task, TaskEvent


async def append_event(task_id: str, event_type: str, payload: dict | None = None) -> int:
    async with SessionLocal() as db:
        row = TaskEvent(
            task_id=task_id,
            event_type=event_type,
            payload=payload or {},
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row.id


async def load_events(task_id: str, after_id: int = 0) -> list[TaskEvent]:
    async with SessionLocal() as db:
        result = await db.execute(
            select(TaskEvent)
            .where(TaskEvent.task_id == task_id, TaskEvent.id > after_id)
            .order_by(TaskEvent.id.asc())
        )
        return list(result.scalars().all())


def serialize_state(state: dict) -> dict:
    return {
        "messages": messages_to_dict(state.get("messages") or []),
        "iteration": state.get("iteration", 0),
        "status": state.get("status", "running"),
        "pending_tool_call_id": state.get("pending_tool_call_id"),
        "sandbox_id": state.get("sandbox_id"),
        "task_id": state.get("task_id"),
    }


def deserialize_state(blob: dict) -> dict:
    data = dict(blob)
    data["messages"] = messages_from_dict(blob.get("messages") or [])
    return data


async def persist_task(task_id: str, *, status: str, state: dict | None = None) -> None:
    async with SessionLocal() as db:
        task = await db.get(Task, task_id)
        if task is None:
            return
        task.status = status
        if state is not None:
            task.agent_state = serialize_state(state)
            task.pending_tool_call_id = state.get("pending_tool_call_id")
        if status in {"done", "failed"}:
            task.completed_at = datetime.now(UTC)
        await db.commit()
