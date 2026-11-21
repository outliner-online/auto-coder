"""Use Auto-Coder's established GitHub request boundary for the daily monitor."""

import httpx

from auto_coder.util.gh_cache import GitDataResponse, GitHubGitDataClient


class MonitorGitHubClient(GitHubGitDataClient):
    """Translate transport unavailability without changing API/schema failures."""

    def request(self, method: str, path: str, payload: dict[str, object] | None = None) -> GitDataResponse:
        try:
            return super().request(method, path, payload)
        except RuntimeError as error:
            if isinstance(error.__cause__, httpx.RequestError):
                raise ConnectionError("GitHub request transport unavailable") from error
            raise


def create_client(token: str, repository: str) -> MonitorGitHubClient:
    return MonitorGitHubClient(token, repository)
