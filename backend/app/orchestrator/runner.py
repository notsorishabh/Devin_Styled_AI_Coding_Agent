from __future__ import annotations

import asyncio
import logging

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage

from app.events import DONE
from app.orchestrator.graph import build_graph
from app.orchestrator.llm import SYSTEM_PROMPT
from app.orchestrator.store import append_event, deserialize_state, persist_task
from app.tools.runtime import ToolContext, build_tools

logger = logging.getLogger(__name__)

_jobs: dict[str, asyncio.Task] = {}


def is_running(task_id: str) -> bool:
    job = _jobs.get(task_id)
    return job is not None and not job.done()


def start_task_job(
    *,
    task_id: str,
    description: str,
    ctx: ToolContext,
    saved_state: dict | None = None,
    resume_answer: str | None = None,
) -> None:
    job = asyncio.create_task(
        _run(
            task_id=task_id,
            description=description,
            ctx=ctx,
            saved_state=saved_state,
            resume_answer=resume_answer,
        )
    )
    _jobs[task_id] = job

    def _clear(done: asyncio.Task) -> None:
        _jobs.pop(task_id, None)
        if done.cancelled():
            return
        exc = done.exception()
        if exc:
            logger.exception("task job crashed id=%s", task_id, exc_info=exc)

    job.add_done_callback(_clear)


async def _run(
    *,
    task_id: str,
    description: str,
    ctx: ToolContext,
    saved_state: dict | None,
    resume_answer: str | None,
) -> None:
    async def emit(event_type: str, payload: dict | None = None) -> None:
        await append_event(task_id, event_type, payload or {})

    tools = build_tools(ctx)
    graph = build_graph(tools, emit)

    if saved_state:
        state = deserialize_state(saved_state)
    else:
        state = {
            "messages": [
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=description),
            ],
            "sandbox_id": ctx.sandbox_id,
            "task_id": task_id,
            "iteration": 0,
            "status": "running",
            "pending_tool_call_id": None,
        }

    if resume_answer is not None:
        pending = state.get("pending_tool_call_id") or "call_ask"
        state["messages"] = list(state["messages"]) + [
            ToolMessage(content=resume_answer, tool_call_id=pending)
        ]
        state["status"] = "running"
        state["pending_tool_call_id"] = None
        await emit("user_answer", {"answer": resume_answer})

    try:
        result = await graph.ainvoke(state)
    except Exception as exc:
        logger.exception("agent loop failed id=%s", task_id)
        await persist_task(task_id, status="failed", state=state)
        await emit(DONE, {"status": "failed", "message": "Agent loop failed"})
        return

    status = result.get("status") or "done"
    await persist_task(task_id, status=status, state=result)
    if status == "waiting_for_user":
        return
    reason = "iteration_cap" if status == "failed" else "completed"
    await emit(DONE, {"status": status, "reason": reason})
