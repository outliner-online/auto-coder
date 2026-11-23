"""Resolve configured transports for provider-owned cloud task handoffs."""

from typing import List, Optional

from .cloud_task_client_base import CloudTaskClientBase


class CloudTaskEngine:
    """Resolve cloud clients without discovering or restarting remote work."""

    def __init__(self, clients: Optional[List[CloudTaskClientBase]] = None) -> None:
        self.clients = clients or []

    def get_client_for_provider(self, provider: str, repo_name: Optional[str] = None, backend_name: Optional[str] = None) -> Optional[CloudTaskClientBase]:
        """Resolve a configured transport by its durable provider identifier."""
        if self.clients:
            provider_types = {"jules": "JulesClient", "claude-routine": "ClaudeRoutineClient", "codex-cloud": "CodexCloudClient"}
            expected_type = provider_types.get(provider)
            candidates = (client for client in self.clients if type(client).__name__ == expected_type)
            if provider == "claude-routine" and backend_name:
                return next((client for client in candidates if getattr(client, "backend_name", None) == backend_name), None)
            return next(candidates, None)
        if provider == "jules":
            from .jules_client import JulesClient

            return JulesClient()
        if provider == "claude-routine":
            from .claude_routine_client import ClaudeRoutineClient

            return ClaudeRoutineClient(backend_name=backend_name or "claude-routine", repo_name=repo_name)
        if provider == "codex-cloud":
            from .codex_cloud_client import CodexCloudClient

            return CodexCloudClient(repo_name=repo_name)
        return None
