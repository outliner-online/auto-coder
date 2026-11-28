import io
import zipfile
from contextlib import contextmanager
from unittest.mock import MagicMock, call, patch

import httpx

from auto_coder.automation_config import AutomationConfig
from auto_coder.github_controller_auth import ControllerCredential
from auto_coder.logger_config import setup_logger
from auto_coder.util.gh_cache import GitHubClient
from auto_coder.util.github_action import (
    _create_github_action_log_summary,
    _get_playwright_artifact_logs,
    filter_actionable_github_checks,
    get_github_actions_logs_from_url,
)
from auto_coder.util.github_request_outcome import github_http_client


def test_job_log_foreign_download_exception_does_not_log_signed_url(tmp_path):
    marker = "signed-job-log-secret"
    console = io.StringIO()
    log_file = tmp_path / "github.log"
    setup_logger(stream=console, log_file=str(log_file))
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.host == "downloads.example":
            raise httpx.ConnectError(f"download failed: {request.url}", request=request)
        if request.url.path.endswith("/logs"):
            return httpx.Response(302, headers={"location": f"https://downloads.example/logs?signature={marker}"}, request=request)
        return httpx.Response(200, json={"name": "tests", "steps": []}, request=request)

    transport = httpx.Client(transport=httpx.MockTransport(handler))
    with patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport):
        result = get_github_actions_logs_from_url(
            "https://github.com/owner/repo/actions/runs/7/job/8",
            github_client=GitHubClient(token="controller-secret"),
        )

    assert "No detailed logs available" in result
    assert requests[-1].url.host == "downloads.example"
    assert "authorization" not in requests[-1].headers
    rendered = console.getvalue() + log_file.read_text()
    assert "category=ConnectError" in rendered
    assert marker not in rendered


def _controller_client() -> MagicMock:
    client = MagicMock()
    client._credential_for.return_value = ControllerCredential("controller-secret", "app", repository="owner/repo")
    return client


def test_invalid_artifact_report_does_not_log_archive_filename(tmp_path):
    marker = "private-archive-payload-marker"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr(f"{marker}.json", "invalid json")
    console = io.StringIO()
    log_file = tmp_path / "github.log"
    setup_logger(stream=console, log_file=str(log_file))

    @contextmanager
    def controlled_http_client(**kwargs):
        transport = MagicMock()
        if kwargs.get("follow_redirects") is False:
            transport.get.return_value = httpx.Response(200, content=archive.getvalue())
        else:
            transport.get.return_value = httpx.Response(200, json={"artifacts": [{"id": 9, "name": "e2e-artifacts-basic", "expired": False}]})
        yield transport

    with patch("auto_coder.util.github_action.github_http_client", controlled_http_client):
        assert _get_playwright_artifact_logs("owner/repo", 7, github_client=_controller_client()) == (None, None)
    rendered = console.getvalue() + log_file.read_text()
    assert "Failed to parse JSON" in rendered
    assert marker not in rendered


def test_artifact_listing_failure_does_not_log_response_payload():
    marker = "private-response-payload"
    response = httpx.Response(403, text=marker)

    @contextmanager
    def controlled_http_client(**_kwargs):
        transport = MagicMock()
        transport.get.return_value = response
        yield transport

    logger = MagicMock()
    with (
        patch("auto_coder.util.github_action.GitHubClient.get_instance", return_value=_controller_client()),
        patch("auto_coder.util.github_action.github_http_client", controlled_http_client),
        patch("auto_coder.util.github_action.logger", logger),
    ):
        assert _get_playwright_artifact_logs("owner/repo", 7) == (None, None)

    diagnostics = " ".join(str(call) for call in logger.method_calls)
    assert marker not in diagnostics
    assert "status=403" in diagnostics


def test_artifact_download_failure_does_not_log_listing_payload(tmp_path):
    marker = "private-artifact-name-marker"
    console = io.StringIO()
    log_file = tmp_path / "github.log"
    setup_logger(stream=console, log_file=str(log_file))

    @contextmanager
    def controlled_http_client(**kwargs):
        transport = MagicMock()
        if kwargs.get("follow_redirects") is False:
            transport.get.return_value = httpx.Response(403)
        else:
            transport.get.return_value = httpx.Response(200, json={"artifacts": [{"id": 9, "name": f"e2e-artifacts-{marker}", "expired": False}]})
        yield transport

    with patch("auto_coder.util.github_action.github_http_client", controlled_http_client):
        assert _get_playwright_artifact_logs("owner/repo", 7, github_client=_controller_client()) == (None, None)

    rendered = console.getvalue() + log_file.read_text()
    assert "403" in rendered
    assert marker not in rendered


def test_foreign_artifact_exception_does_not_log_signed_url():
    marker = "signed-secret-marker"
    listing = httpx.Response(200, json={"artifacts": [{"id": 9, "name": "e2e-artifacts-basic", "expired": False}]})
    redirect = httpx.Response(302, headers={"location": f"https://downloads.example/archive?signature={marker}"})

    @contextmanager
    def controlled_http_client(**kwargs):
        transport = MagicMock()
        if kwargs.get("follow_redirects") is False:
            transport.get.side_effect = [redirect, httpx.ConnectError(f"failed {marker}")]
        else:
            transport.get.return_value = listing
        yield transport

    logger = MagicMock()
    with (
        patch("auto_coder.util.github_action.GitHubClient.get_instance", return_value=_controller_client()),
        patch("auto_coder.util.github_action.github_http_client", controlled_http_client),
        patch("auto_coder.util.github_action.logger", logger),
    ):
        assert _get_playwright_artifact_logs("owner/repo", 7) == (None, None)

    diagnostics = " ".join(str(call) for call in logger.method_calls)
    assert marker not in diagnostics
    assert "category=ConnectError" in diagnostics


def test_governed_artifact_failure_suppresses_response_message(tmp_path):
    marker = "artifact-response-private-marker"
    console = io.StringIO()
    log_file = tmp_path / "github.log"
    setup_logger(stream=console, log_file=str(log_file))
    observations = []

    with github_http_client(
        subsystem="actions-artifacts",
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                403,
                json={"message": marker, "documentation_url": f"https://docs.example/{marker}"},
                request=request,
            )
        ),
        observation_hook=observations.append,
    ) as client:
        response = client.get(
            "https://api.github.com/repos/owner/repo/actions/runs/7/artifacts",
            headers={"Authorization": "Bearer controller-secret"},
        )

    assert response.status_code == 403
    rendered = console.getvalue() + log_file.read_text()
    assert marker not in rendered
    assert len(observations) == 1
    assert observations[0].status == 403
    assert observations[0].context.subsystem == "actions-artifacts"
    assert observations[0].message is None
    assert observations[0].documentation_url is None


def test_artifact_requests_use_injected_client_and_renew_at_each_api_send():
    credentials = iter(("controller-one", "controller-two"))
    controller = MagicMock()
    controller._credential_for.side_effect = lambda *_args: ControllerCredential(next(credentials), "controller-app", repository="owner/repo")
    seen_authorization = []
    listing = httpx.Response(
        200,
        json={"artifacts": [{"id": 9, "name": "e2e-artifacts-basic", "expired": False}]},
    )
    failed_download = httpx.Response(403, json={"message": "denied"})

    @contextmanager
    def controlled_http_client(**kwargs):
        transport = MagicMock()

        def get(_url, **request_kwargs):
            seen_authorization.append(request_kwargs.get("headers", {}).get("Authorization"))
            return failed_download if kwargs.get("follow_redirects") is False else listing

        transport.get.side_effect = get
        yield transport

    with (
        patch(
            "auto_coder.util.github_action.GitHubClient.get_instance",
            side_effect=AssertionError("legacy singleton must not be selected"),
        ),
        patch("auto_coder.util.github_action.github_http_client", controlled_http_client),
    ):
        assert _get_playwright_artifact_logs("owner/repo", 7, github_client=controller) == (
            None,
            None,
        )

    assert seen_authorization == ["Bearer controller-one", "Bearer controller-two"]


def test_log_summary_threads_injected_client_to_artifacts_sorting_and_job_logs():
    controller = MagicMock()
    controller.api_for.return_value = MagicMock()
    checks = [
        {
            "name": "tests",
            "details_url": "https://github.com/owner/repo/actions/runs/7/job/8",
        }
    ]
    jobs = [
        {
            "name": "tests",
            "conclusion": "failure",
            "html_url": "https://github.com/owner/repo/actions/runs/7/job/8",
        }
    ]

    with (
        patch(
            "auto_coder.util.github_action.GitHubClient.get_instance",
            side_effect=AssertionError("legacy singleton must not be selected"),
        ),
        patch(
            "auto_coder.util.github_action.filter_actionable_github_checks",
            side_effect=lambda _repo, values, **_kwargs: values,
        ),
        patch(
            "auto_coder.util.github_action._get_playwright_artifact_logs",
            return_value=(None, []),
        ) as artifacts,
        patch("auto_coder.util.github_action.list_all_workflow_jobs", return_value=jobs),
        patch("auto_coder.util.github_action._sort_jobs_by_workflow", return_value=jobs) as sorting,
        patch(
            "auto_coder.util.github_action.get_github_actions_logs_from_url",
            return_value="FAILED tests/test_ci.py::test_controller",
        ) as logs,
    ):
        summary, files = _create_github_action_log_summary("owner/repo", AutomationConfig(), checks, github_client=controller)

    assert "test_controller" in summary
    assert files == ["tests/test_ci.py"]
    artifacts.assert_called_once_with("owner/repo", 7, github_client=controller)
    sorting.assert_called_once_with(jobs, "owner", "repo", 7, github_client=controller)
    logs.assert_called_once_with(jobs[0]["html_url"], github_client=controller)


def test_advisory_filter_uses_injected_repository_client_without_singleton_fallback():
    repository = "owner/repo"
    head = "a" * 40

    def workflow(run_id, path):
        return {
            "id": run_id,
            "run_attempt": 1,
            "head_sha": head,
            "path": path,
            "repository": {"full_name": repository},
        }

    diagnostic = workflow(30, ".github/workflows/test-impact-observation.yml")
    ordinary = workflow(20, ".github/workflows/pr-tests.yml")
    api = MagicMock()
    api.actions.get_workflow_run.side_effect = [diagnostic, ordinary]

    class Controller:
        def __init__(self):
            self.api_calls = MagicMock()

        def api_for(self, repo_name, **kwargs):
            self.api_calls(repo_name, **kwargs)
            return api

    controller = Controller()
    checks = [
        {
            "id": check_id,
            "details_url": f"https://github.com/{repository}/actions/runs/{run_id}/job/{check_id}",
        }
        for check_id, run_id in ((31, 30), (21, 20))
    ]

    with patch(
        "auto_coder.util.github_action.GitHubClient.get_instance",
        side_effect=AssertionError("legacy singleton must not be selected"),
    ):
        remaining = filter_actionable_github_checks(repository, checks, github_client=controller)

    assert [check["id"] for check in remaining] == [21]
    controller.api_calls.assert_called_once_with(
        repository,
        required_permissions={"actions": "read"},
    )
    assert api.actions.get_workflow_run.call_args_list == [
        call("owner", "repo", 30),
        call("owner", "repo", 20),
    ]
