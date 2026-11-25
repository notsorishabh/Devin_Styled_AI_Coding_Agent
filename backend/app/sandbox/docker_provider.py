"""DockerSandboxProvider.

Clone strategy: `git clone` runs *inside* the container (not a host bind-mount).
The repo lives on a named Docker volume mounted at /workspace. destroy()
removes the container, that volume, and any in-memory persistent shell sessions.

Shell sessions (Step 3) are in-memory only — not written to Postgres. They last
for the life of this process and are torn down with the sandbox.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from docker.types import Mount

from app.config import settings
from app.sandbox.provider import (
    ExecResult,
    GitAuthError,
    SandboxError,
    SandboxNotFound,
    SandboxProvider,
    ShellNotFound,
)
from app.sandbox.shell import ShellRegistry, _raw_socket, open_session

logger = logging.getLogger(__name__)

LABEL_SANDBOX_ID = "devin.sandbox_id"
LABEL_VOLUME = "devin.volume"
WORKSPACE = "/workspace"
IMAGE_CONTEXT = Path(__file__).resolve().parents[3] / "sandbox-image"
PATH_PYTHON = r"""python3 - <<'PY'
import os, sys, base64
from pathlib import Path
root = Path("/workspace/repo").resolve()
action = os.environ.get("DEVIN_ACTION", "")
raw = os.environ.get("DEVIN_PATH", ".") or "."
target = Path(raw)
if not target.is_absolute():
    target = root / target
target = target.resolve()
if target != root and root not in target.parents:
    print("path is outside the workspace", file=sys.stderr)
    sys.exit(1)
if action == "read":
    if not target.is_file():
        print("file not found", file=sys.stderr)
        sys.exit(1)
    sys.stdout.write(target.read_text(encoding="utf-8", errors="replace"))
elif action == "write":
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(os.environ.get("DEVIN_B64", "")))
elif action == "list":
    if not target.is_dir():
        print("directory not found", file=sys.stderr)
        sys.exit(1)
    for name in sorted(os.listdir(target)):
        print(name)
else:
    print("unknown action", file=sys.stderr)
    sys.exit(1)
PY"""


class DockerSandboxProvider(SandboxProvider):
    def __init__(self) -> None:
        self._client = None
        self._shells = ShellRegistry()

    def _docker(self):
        if self._client is None:
            try:
                self._client = docker.from_env()
                self._client.ping()
            except DockerException as exc:
                self._client = None
                raise SandboxError("Docker daemon is not available") from exc
        return self._client

    def _ensure_image(self) -> str:
        image = settings.sandbox_image
        client = self._docker()
        try:
            client.images.get(image)
            return image
        except ImageNotFound:
            logger.info("Building sandbox image %s from %s", image, IMAGE_CONTEXT)
            client.images.build(
                path=str(IMAGE_CONTEXT),
                tag=image,
                rm=True,
            )
            return image

    def _container(self, sandbox_id: str, *, running_only: bool = False):
        filters = {"label": f"{LABEL_SANDBOX_ID}={sandbox_id}"}
        found = self._docker().containers.list(all=not running_only, filters=filters)
        if not found:
            raise SandboxNotFound(sandbox_id)
        return found[0]

    def create_sandbox(self, repo_url: str, resource_limits: dict) -> str:
        sandbox_id = str(uuid.uuid4())
        cpus = float(resource_limits.get("cpus", settings.sandbox_cpus))
        memory = str(resource_limits.get("memory", settings.sandbox_memory))
        image = self._ensure_image()

        logger.info(
            "sandbox creating id=%s repo=%s", sandbox_id, redact_credentials(repo_url)
        )
        volume_name = f"devin-ws-{sandbox_id[:8]}"
        try:
            self._docker().volumes.create(name=volume_name)
            container = self._docker().containers.run(
                image,
                command=["sleep", "infinity"],
                detach=True,
                name=f"devin-{sandbox_id[:8]}",
                labels={LABEL_SANDBOX_ID: sandbox_id, LABEL_VOLUME: volume_name},
                nano_cpus=int(cpus * 1_000_000_000),
                mem_limit=memory,
                memswap_limit=memory,
                mounts=[
                    Mount(target=WORKSPACE, source=volume_name, type="volume"),
                ],
                working_dir=WORKSPACE,
            )
        except DockerException as exc:
            logger.exception("sandbox container start failed id=%s", sandbox_id)
            self._remove_volume(volume_name)
            raise SandboxError("Failed to start sandbox container") from exc

        try:
            self._clone_repo(container, repo_url, sandbox_id)
        except GitAuthError:
            self._remove_container(container)
            raise
        except Exception:
            self._remove_container(container)
            logger.exception("sandbox clone error id=%s", sandbox_id)
            raise SandboxError("Failed to clone repository into sandbox") from None

        logger.info(
            "sandbox created id=%s container=%s",
            sandbox_id,
            container.short_id,
        )
        return sandbox_id

    def exec(self, sandbox_id: str, command: str) -> dict:
        try:
            container = self._container(sandbox_id)
        except SandboxNotFound:
            raise
        except DockerException as exc:
            raise SandboxError("Docker daemon is not available") from exc

        if container.status != "running":
            raise SandboxNotFound(sandbox_id)

        result = self._exec_raw(container, command, workdir=f"{WORKSPACE}/repo")
        logger.info(
            "sandbox exec id=%s exit=%s",
            sandbox_id,
            result.exit_code,
        )
        return result.as_dict()

    def destroy(self, sandbox_id: str) -> None:
        self._shells.close_for_sandbox(sandbox_id)
        try:
            container = self._container(sandbox_id)
        except SandboxNotFound:
            logger.info("sandbox destroy id=%s already gone", sandbox_id)
            return
        except DockerException as exc:
            raise SandboxError("Docker daemon is not available") from exc

        self._remove_container(container)
        logger.info("sandbox destroyed id=%s", sandbox_id)

    def open_shell(self, sandbox_id: str) -> str:
        try:
            container = self._container(sandbox_id)
        except SandboxNotFound:
            raise
        except DockerException as exc:
            raise SandboxError("Docker daemon is not available") from exc
        if container.status != "running":
            raise SandboxNotFound(sandbox_id)

        client = self._docker()
        try:
            exec_id = client.api.exec_create(
                container.id,
                cmd=["bash", "-i"],
                stdin=True,
                tty=True,
                stdout=True,
                stderr=True,
                workdir=f"{WORKSPACE}/repo",
            )["Id"]
            sock = client.api.exec_start(
                exec_id, tty=True, stream=True, socket=True
            )
        except (APIError, DockerException) as exc:
            raise SandboxError("Failed to open sandbox shell") from exc

        session = open_session(sandbox_id, sock, self._shells)
        return session.id

    def latest_shell_id(self, sandbox_id: str) -> str:
        return self._shells.latest_open(sandbox_id).id

    def send_command(self, shell_session_id: str, command: str) -> None:
        session = self._shells.get(shell_session_id)
        payload = command if command.endswith("\n") else command + "\n"
        raw = _raw_socket(session.sock)
        data = payload.encode("utf-8")
        try:
            if hasattr(raw, "sendall"):
                raw.sendall(data)
            elif hasattr(raw, "send"):
                raw.send(data)
            elif hasattr(raw, "write"):
                raw.write(data)
            else:
                raise SandboxError("Shell socket is not writable")
        except OSError as exc:
            self._shells.close(shell_session_id)
            raise ShellNotFound(shell_session_id) from exc
        logger.info("shell command id=%s", shell_session_id)

    async def stream_output(
        self, shell_session_id: str, last_event_id: int = 0
    ) -> AsyncIterator[tuple[int, str]]:
        session = self._shells.get(shell_session_id)
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        with session.lock:
            session.listeners.append((loop, queue))
            replay = [
                (eid, text)
                for eid, text in session.buffer
                if eid > last_event_id
            ]
        max_id = last_event_id
        try:
            for eid, text in replay:
                max_id = eid
                yield eid, text
            while True:
                item = await queue.get()
                if item is None:
                    break
                eid, text = item
                if eid > max_id:
                    max_id = eid
                    yield eid, text
        finally:
            with session.lock:
                session.listeners = [
                    pair for pair in session.listeners if pair[1] is not queue
                ]

    def _clone_repo(self, container, repo_url: str, sandbox_id: str) -> None:
        clone = self._exec_raw(
            container,
            f"git clone --depth 1 {sh_single_quote(repo_url)} {WORKSPACE}/repo",
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
        if clone.exit_code != 0:
            detail = redact_credentials(f"{clone.stdout}\n{clone.stderr}")
            logger.error(
                "sandbox clone failed id=%s exit=%s",
                sandbox_id,
                clone.exit_code,
            )
            if _looks_like_git_auth_failure(detail):
                raise GitAuthError("Please re-authenticate with GitHub")
            raise SandboxError("Failed to clone repository into sandbox")

        origin = origin_without_credentials(repo_url)
        rewrite = self._exec_raw(
            container,
            f"git -C {WORKSPACE}/repo remote set-url origin {sh_single_quote(origin)}",
        )
        if rewrite.exit_code != 0:
            raise SandboxError("Failed to clone repository into sandbox")
        self._exec_raw(
            container,
            "git -C /workspace/repo config user.email 'devin-agent@local' "
            "&& git -C /workspace/repo config user.name 'Devin Agent'",
        )

    def docker_container_id(self, sandbox_id: str) -> str | None:
        try:
            return self._container(sandbox_id).id
        except SandboxNotFound:
            return None

    def read_file(self, sandbox_id: str, path: str) -> str:
        result = self._path_python(
            sandbox_id,
            "read",
            path,
        )
        if result.exit_code != 0:
            raise SandboxError((result.stderr or result.stdout or "Failed to read file").strip())
        return result.stdout

    def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        import base64

        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        result = self._path_python(sandbox_id, "write", path, extra_env={"DEVIN_B64": encoded})
        if result.exit_code != 0:
            raise SandboxError((result.stderr or result.stdout or "Failed to write file").strip())

    def list_dir(self, sandbox_id: str, path: str = ".") -> list[str]:
        result = self._path_python(sandbox_id, "list", path or ".")
        if result.exit_code != 0:
            raise SandboxError((result.stderr or result.stdout or "Failed to list directory").strip())
        lines = [line for line in result.stdout.splitlines() if line]
        return lines

    def _path_python(
        self,
        sandbox_id: str,
        action: str,
        path: str,
        extra_env: dict | None = None,
    ) -> ExecResult:
        container = self._container(sandbox_id)
        if container.status != "running":
            raise SandboxNotFound(sandbox_id)
        env = {"DEVIN_ACTION": action, "DEVIN_PATH": path, **(extra_env or {})}
        return self._exec_raw(container, PATH_PYTHON, workdir=f"{WORKSPACE}/repo", env=env)

    def _exec_raw(
        self,
        container,
        command: str,
        workdir: str = WORKSPACE,
        env: dict | None = None,
    ) -> ExecResult:
        try:
            result = container.exec_run(
                ["sh", "-c", command],
                demux=True,
                workdir=workdir,
                environment=env,
            )
        except NotFound as exc:
            raise SandboxNotFound(container.id) from exc
        except APIError as exc:
            raise SandboxError("Failed to exec in sandbox") from exc

        out_b, err_b = result.output if result.output else (b"", b"")
        stdout = (out_b or b"").decode("utf-8", errors="replace")
        stderr = (err_b or b"").decode("utf-8", errors="replace")
        return ExecResult(stdout=stdout, stderr=stderr, exit_code=int(result.exit_code))

    def _remove_container(self, container) -> None:
        volume_name = None
        try:
            volume_name = container.labels.get(LABEL_VOLUME)
        except Exception:
            volume_name = None
        try:
            container.stop(timeout=5)
        except (NotFound, APIError):
            pass
        try:
            container.remove(v=True, force=True)
        except NotFound:
            pass
        except APIError as exc:
            logger.warning("sandbox remove incomplete: %s", exc)
        if volume_name:
            self._remove_volume(volume_name)

    def _remove_volume(self, volume_name: str) -> None:
        try:
            self._docker().volumes.get(volume_name).remove(force=True)
        except (NotFound, APIError, DockerException):
            pass


_CRED_IN_URL = re.compile(r"(https?://)([^/@\s]+):([^@/\s]+)@", re.IGNORECASE)
_AUTH_HINTS = (
    "authentication failed",
    "invalid username",
    "could not read username",
    "could not read password",
    "access denied",
    "permission denied (publickey)",
    "fatal: could not read",
    "repository not found",
)


def redact_credentials(text: str) -> str:
    return _CRED_IN_URL.sub(r"\1\2:****@", text)


def origin_without_credentials(repo_url: str) -> str:
    parts = urlsplit(repo_url)
    host = parts.hostname or "github.com"
    netloc = f"{host}:{parts.port}" if parts.port else host
    return urlunsplit((parts.scheme or "https", netloc, parts.path, "", ""))


def _looks_like_git_auth_failure(text: str) -> bool:
    lowered = text.lower()
    return any(hint in lowered for hint in _AUTH_HINTS)


def sh_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"
