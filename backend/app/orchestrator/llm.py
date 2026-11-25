from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

from app.config import settings

SYSTEM_PROMPT = """You are a coding agent working in a cloned git repository inside a sandbox.
The workspace is /workspace/repo. Use tools to inspect and change files.
Use shell_exec for tests, git diff, and other commands.
When you need a human decision, call ask_user alone (no other tools in that turn).
When the work is done, either commit/push/open a PR if that is appropriate, or explain clearly why you could not.
Do not loop on the same failing command. Prefer small, concrete changes.
"""


class LLMNotConfigured(RuntimeError):
    pass


class ScriptedDevinModel(BaseChatModel):
    """Deterministic stand-in so the loop, SSE, and ask_user can be tested without an API key."""

    @property
    def _llm_type(self) -> str:
        return "devin-scripted"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, **kwargs):
        return ChatResult(generations=[ChatGeneration(message=self._next(messages))])

    async def _agenerate(self, messages, stop=None, **kwargs):
        return self._generate(messages, stop, **kwargs)

    def _next(self, messages: list[BaseMessage]) -> AIMessage:
        tool_results = [m for m in messages if isinstance(m, ToolMessage)]
        if len(tool_results) == 0:
            return AIMessage(
                content="I'll list the repository files first.",
                tool_calls=[
                    {
                        "name": "list_dir",
                        "args": {"path": "."},
                        "id": "call_list",
                        "type": "tool_call",
                    }
                ],
            )
        if len(tool_results) == 1:
            return AIMessage(
                content="I need a quick decision from you before continuing.",
                tool_calls=[
                    {
                        "name": "ask_user",
                        "args": {
                            "question": "Scripted agent pause: reply with any short answer to resume."
                        },
                        "id": "call_ask",
                        "type": "tool_call",
                    }
                ],
            )
        return AIMessage(
            content="Done. I listed the repo, asked you a question, and received your answer."
        )


def get_chat_model() -> BaseChatModel:
    provider = (settings.llm_provider or "openai").lower()
    if provider == "fake":
        return ScriptedDevinModel()
    if not settings.llm_api_key:
        raise LLMNotConfigured("LLM_API_KEY is not set")
    kwargs: dict = {
        "model": settings.llm_model,
        "api_key": settings.llm_api_key,
        "temperature": 0,
    }
    if settings.llm_base_url:
        kwargs["base_url"] = settings.llm_base_url
    return ChatOpenAI(**kwargs)
