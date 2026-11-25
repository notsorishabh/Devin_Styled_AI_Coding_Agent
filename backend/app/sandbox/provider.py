from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int

    def as_dict(self) -> dict:
        return {
            "stdout": self.stdout,
            "stderr": self.stderr,
            "exit_code": self.exit_code,
        }


class SandboxError(Exception):
    """Raised when the sandbox backing fails (start, clone, docker daemon, …)."""


class GitAuthError(SandboxError):
    """Clone failed because the GitHub token is expired, revoked, or invalid."""


class SandboxNotFound(SandboxError):
    """Raised when exec/destroy targets a sandbox that is gone or unknown."""


class ShellNotFound(SandboxError):
    """Raised when a persistent shell session is missing or already closed."""


class SandboxProvider(ABC):
    """Step 2 one-shot exec plus Step 3 persistent shell. No Docker types leak out."""

    @abstractmethod
    def create_sandbox(self, repo_url: str, resource_limits: dict) -> str:
        """Spin up an isolated env, clone repo_url into it, return sandbox_id."""

    @abstractmethod
    def exec(self, sandbox_id: str, command: str) -> dict:
        """Run a one-off command. Returns {stdout, stderr, exit_code}."""

    @abstractmethod
    def destroy(self, sandbox_id: str) -> None:
        """Stop and remove the sandbox. Idempotent if already gone."""

    @abstractmethod
    def open_shell(self, sandbox_id: str) -> str:
        """Spawn a persistent shell in the container. Returns shell_session_id."""

    @abstractmethod
    def send_command(self, shell_session_id: str, command: str) -> None:
        """Write to the shell's stdin without waiting for the command to finish."""

    @abstractmethod
    def stream_output(
        self, shell_session_id: str, last_event_id: int = 0
    ) -> AsyncIterator[tuple[int, str]]:
        """Yield (event_id, chunk) as output arrives, after replaying buffered ids."""

    @abstractmethod
    def read_file(self, sandbox_id: str, path: str) -> str:
        """Read a text file from the sandbox workspace."""

    @abstractmethod
    def write_file(self, sandbox_id: str, path: str, content: str) -> None:
        """Create or overwrite a text file in the sandbox workspace."""

    @abstractmethod
    def list_dir(self, sandbox_id: str, path: str = ".") -> list[str]:
        """List directory entries in the sandbox workspace."""
