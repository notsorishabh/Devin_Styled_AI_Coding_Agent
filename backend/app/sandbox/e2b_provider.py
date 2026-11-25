from collections.abc import AsyncIterator

from app.sandbox.provider import SandboxProvider


class E2BSandboxProvider(SandboxProvider):
    """V2 stub. Same interface as Docker; not implemented in V1."""

    def create_sandbox(self, repo_url: str, resource_limits: dict) -> str:
        raise NotImplementedError("E2BSandboxProvider is V2")

    def exec(self, sandbox_id: str, command: str) -> dict:
        raise NotImplementedError("E2BSandboxProvider is V2")

    def destroy(self, sandbox_id: str) -> None:
        raise NotImplementedError("E2BSandboxProvider is V2")

    def open_shell(self, sandbox_id: str) -> str:
        raise NotImplementedError("E2BSandboxProvider is V2")

    def send_command(self, shell_session_id: str, command: str) -> None:
        raise NotImplementedError("E2BSandboxProvider is V2")

    def stream_output(
        self, shell_session_id: str, last_event_id: int = 0
    ) -> AsyncIterator[tuple[int, str]]:
        raise NotImplementedError("E2BSandboxProvider is V2")
        yield  # pragma: no cover

    def read_file(self, sandbox_id: str, path: str) -> str:
        raise NotImplementedError("E2BSandboxProvider is V2")

    def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        raise NotImplementedError("E2BSandboxProvider is V2")

    def list_dir(self, sandbox_id: str, path: str = ".") -> list[str]:
        raise NotImplementedError("E2BSandboxProvider is V2")
