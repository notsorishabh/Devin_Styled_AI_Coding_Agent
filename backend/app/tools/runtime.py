from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass

from langchain_core.tools import StructuredTool
from pydantic import BaseModel

from app.github import (
    authenticated_clone_url,
    create_github_pull_request,
    github_repo_metadata,
    repo_full_name_from_url,
)
from app.sandbox.docker_provider import redact_credentials, sh_single_quote
from app.sandbox.provider import SandboxError, SandboxProvider

RESULT_LIMIT = 8000


@dataclass
class ToolContext:
    sandbox_id: str
    provider: SandboxProvider
    github_token: str
    repo_url: str
    github_username: str


class ShellArgs(BaseModel):
    command: str


class PathArgs(BaseModel):
    path: str


class ListDirArgs(BaseModel):
    path: str = "."


class WriteFileArgs(BaseModel):
    path: str
    content: str


class CommitArgs(BaseModel):
    message: str


class PullRequestArgs(BaseModel):
    title: str
    body: str = ""


class EmptyArgs(BaseModel):
    pass


class AskUserArgs(BaseModel):
    question: str


def _clip(text: str) -> str:
    if len(text) <= RESULT_LIMIT:
        return text
    return text[: RESULT_LIMIT] + "\n… truncated"


def build_tools(ctx: ToolContext) -> list:
    async def shell_exec(command: str) -> str:
        result = await asyncio.to_thread(ctx.provider.exec, ctx.sandbox_id, command)
        return _clip(redact_credentials(json.dumps(result)))

    async def read_file(path: str) -> str:
        try:
            return _clip(await asyncio.to_thread(ctx.provider.read_file, ctx.sandbox_id, path))
        except SandboxError as exc:
            return f"error: {exc}"

    async def write_file(path: str, content: str) -> str:
        try:
            await asyncio.to_thread(ctx.provider.write_file, ctx.sandbox_id, path, content)
        except SandboxError as exc:
            return f"error: {exc}"
        return f"wrote {path}"

    async def list_dir(path: str = ".") -> str:
        try:
            entries = await asyncio.to_thread(ctx.provider.list_dir, ctx.sandbox_id, path)
        except SandboxError as exc:
            return f"error: {exc}"
        return _clip("\n".join(entries))

    async def git_commit(message: str) -> str:
        quoted = sh_single_quote(message)
        result = await asyncio.to_thread(
            ctx.provider.exec,
            ctx.sandbox_id,
            f"git add -A && git commit -m {quoted}",
        )
        return _clip(json.dumps(result))

    async def git_push() -> str:
        full_name = repo_full_name_from_url(ctx.repo_url)
        branch = await asyncio.to_thread(
            ctx.provider.exec, ctx.sandbox_id, "git rev-parse --abbrev-ref HEAD"
        )
        if branch.get("exit_code") != 0:
            return _clip(json.dumps(branch))
        name = (branch.get("stdout") or "HEAD").strip()
        if not re.fullmatch(r"[A-Za-z0-9._/\-]+", name):
            return "error: unexpected git branch name"
        remote = authenticated_clone_url(full_name, ctx.github_token)
        result = await asyncio.to_thread(
            ctx.provider.exec,
            ctx.sandbox_id,
            f"git push {sh_single_quote(remote)} HEAD:refs/heads/{name}",
        )
        blob = redact_credentials(json.dumps(result))
        return _clip(blob)

    async def create_pr(title: str, body: str = "") -> str:
        full_name = repo_full_name_from_url(ctx.repo_url)
        branch = await asyncio.to_thread(
            ctx.provider.exec, ctx.sandbox_id, "git rev-parse --abbrev-ref HEAD"
        )
        if branch.get("exit_code") != 0:
            return _clip(json.dumps(branch))
        head = (branch.get("stdout") or "").strip()
        try:
            meta = await github_repo_metadata(ctx.github_token, full_name)
            pr = await create_github_pull_request(
                ctx.github_token,
                full_name,
                title=title,
                body=body,
                head=head,
                base=meta["default_branch"],
            )
        except Exception as exc:
            return f"error: {exc}"
        return json.dumps(pr)

    async def ask_user(question: str) -> str:
        return question

    return [
        StructuredTool.from_function(
            coroutine=shell_exec,
            name="shell_exec",
            description="Run a command in the sandbox workspace. Cwd is the cloned repo.",
            args_schema=ShellArgs,
        ),
        StructuredTool.from_function(
            coroutine=read_file,
            name="read_file",
            description="Read a text file from the sandbox workspace.",
            args_schema=PathArgs,
        ),
        StructuredTool.from_function(
            coroutine=write_file,
            name="write_file",
            description="Create or overwrite a text file in the sandbox workspace.",
            args_schema=WriteFileArgs,
        ),
        StructuredTool.from_function(
            coroutine=list_dir,
            name="list_dir",
            description="List entries in a directory in the sandbox workspace.",
            args_schema=ListDirArgs,
        ),
        StructuredTool.from_function(
            coroutine=git_commit,
            name="git_commit",
            description="Stage all changes and create a git commit in the sandbox clone.",
            args_schema=CommitArgs,
        ),
        StructuredTool.from_function(
            coroutine=git_push,
            name="git_push",
            description="Push the current branch to GitHub for this session's repo.",
            args_schema=EmptyArgs,
        ),
        StructuredTool.from_function(
            coroutine=create_pr,
            name="create_pr",
            description="Open a GitHub pull request for the current branch via the GitHub API.",
            args_schema=PullRequestArgs,
        ),
        StructuredTool.from_function(
            coroutine=ask_user,
            name="ask_user",
            description="Pause the agent and ask the human a question. Use this alone, with no other tools in the same turn.",
            args_schema=AskUserArgs,
        ),
    ]
