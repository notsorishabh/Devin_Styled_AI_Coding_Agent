from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from app.config import settings
from app.events import REASONING, TOOL_CALL, TOOL_RESULT, USER_PROMPT
from app.orchestrator.llm import get_chat_model
from app.sandbox.docker_provider import redact_credentials

MAX_RESULT = 8000


class AgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    sandbox_id: str
    task_id: str
    iteration: int
    status: str
    pending_tool_call_id: str | None


def _content_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    return str(content)


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_RESULT:
        return value[:MAX_RESULT] + "\n… truncated"
    return value


def build_graph(tools: list, emit, max_iterations: int | None = None):
    max_iterations = max_iterations or settings.agent_max_iterations
    llm = get_chat_model()
    by_name = {tool.name: tool for tool in tools}

    async def agent_reasoning(state: AgentState) -> dict:
        if state.get("status") in {"done", "failed", "waiting_for_user"}:
            return {}
        if state["iteration"] >= max_iterations:
            return {
                "status": "failed",
                "messages": [
                    AIMessage(
                        content="Stopped: iteration cap reached. No further tool calls will run."
                    )
                ],
            }
        model = llm.bind_tools(tools)
        response = await model.ainvoke(state["messages"])
        text = _content_text(response).strip()
        if text:
            await emit(REASONING, {"text": text})
        if not getattr(response, "tool_calls", None):
            return {"messages": [response], "status": "done"}
        return {"messages": [response]}

    async def tool_execution(state: AgentState) -> dict:
        last = state["messages"][-1]
        tool_calls = getattr(last, "tool_calls", None) or []
        outputs: list[ToolMessage] = []
        iteration = state["iteration"] + 1
        if iteration > max_iterations:
            await emit(
                REASONING,
                {"text": "Stopped: iteration cap reached."},
            )
            return {
                "status": "failed",
                "iteration": iteration,
                "messages": [
                    AIMessage(content="Stopped: iteration cap reached.")
                ],
            }

        for call in tool_calls:
            name = call["name"]
            args = call.get("args") or {}
            call_id = call["id"]
            await emit(
                TOOL_CALL,
                {"tool": name, "args": _clip(args)},
            )
            if name == "ask_user":
                question = args.get("question") or ""
                await emit(USER_PROMPT, {"question": question})
                return {
                    "status": "waiting_for_user",
                    "iteration": iteration,
                    "pending_tool_call_id": call_id,
                    "messages": outputs,
                }
            tool = by_name.get(name)
            if tool is None:
                result = f"unknown tool: {name}"
            else:
                try:
                    result = await tool.ainvoke(args)
                except Exception as exc:
                    result = f"error: {exc}"
            text = redact_credentials(_clip(str(result)))
            await emit(TOOL_RESULT, {"tool": name, "result": text})
            outputs.append(ToolMessage(content=text, tool_call_id=call_id))

        status = "failed" if iteration >= max_iterations else "running"
        if status == "failed":
            await emit(
                REASONING,
                {"text": "Stopped: iteration cap reached."},
            )
        return {
            "messages": outputs,
            "iteration": iteration,
            "status": status,
            "pending_tool_call_id": None,
        }

    def after_agent(state: AgentState):
        if state.get("status") in {"done", "failed", "waiting_for_user"}:
            return END
        last = state["messages"][-1]
        if getattr(last, "tool_calls", None):
            return "tool_execution"
        return END

    def after_tools(state: AgentState):
        if state.get("status") in {"done", "failed", "waiting_for_user"}:
            return END
        return "agent_reasoning"

    graph = StateGraph(AgentState)
    graph.add_node("agent_reasoning", agent_reasoning)
    graph.add_node("tool_execution", tool_execution)
    graph.add_edge(START, "agent_reasoning")
    graph.add_conditional_edges("agent_reasoning", after_agent)
    graph.add_conditional_edges("tool_execution", after_tools)
    return graph.compile()
