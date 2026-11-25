from threading import Lock

from app.config import settings
from app.sandbox.docker_provider import DockerSandboxProvider
from app.sandbox.e2b_provider import E2BSandboxProvider
from app.sandbox.provider import SandboxProvider

_lock = Lock()
_provider: SandboxProvider | None = None


def get_sandbox_provider() -> SandboxProvider:
    """Process-wide singleton so persistent shell sockets survive across requests."""
    global _provider
    with _lock:
        if _provider is None:
            kind = settings.sandbox_provider
            if kind == "docker":
                _provider = DockerSandboxProvider()
            elif kind == "e2b":
                _provider = E2BSandboxProvider()
            else:
                raise ValueError(f"Unknown SANDBOX_PROVIDER={kind}")
        return _provider
