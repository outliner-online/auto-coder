import base64
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

from src.auto_coder.automation_config import AutomationConfig
from src.auto_coder.dispatch_claim_store import DispatchClaimStore, DispatchOutcome
from src.auto_coder.entity_invalidation import DurableInvalidationQueue
from src.auto_coder.github_controller_auth import ControllerCredential, ControllerIdentity
from src.auto_coder.pr_processor import _handle_pr_merge
from src.auto_coder.pr_progress_policy import CIEvidenceState, ProgressRoute, PRProgressDecision
from src.auto_coder.util.gh_cache import GitHubClient
from src.auto_coder.util.github_action import WorkflowDispatchResult
from tests.ci_observation_fakes import authoritative_ci_status


def test_dispatch_creates_restart_safe_watch_and_claim():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        claims = DispatchClaimStore(root / "claims.db")
        client = MagicMock()
        client.get_authenticated_controller_identity.return_value = ControllerIdentity("controller", account_id=701)
        client.get_pr_review_threads_strict.return_value = []
        pr = {"number": 123, "head": {"sha": "abc1234", "ref": "feature"}, "labels": []}
        with (
            patch.dict(os.environ, {"AUTO_CODER_INVALIDATION_DB": str(root / "invalidations.db")}),
            patch("src.auto_coder.pr_processor.get_dispatch_claim_store", return_value=claims),
            patch("src.auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[])),
            patch("src.auto_coder.pr_processor.get_detailed_checks_from_history"),
            patch("src.auto_coder.pr_processor.LabelManager"),
            patch("auto_coder.util.github_action.trigger_workflow_dispatch", return_value=WorkflowDispatchResult(outcome=DispatchOutcome.ACCEPTED)) as dispatch,
        ):
            actions = _handle_pr_merge(client, "owner/repo", pr, AutomationConfig(), {})
            _handle_pr_merge(client, "owner/repo", pr, AutomationConfig(), {})

        assert dispatch.call_count == 0
        assert any("No current CI observations are available for PR #123" in action for action in actions)
        reopened = DurableInvalidationQueue(root / "invalidations.db")
        assert reopened.seconds_until_next_ci("owner/repo") == 0


def test_dispatch_admission_forwards_the_injected_controller_client():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        claims = DispatchClaimStore(root / "claims.db")
        controller = MagicMock(name="controller-client")
        controller.get_authenticated_controller_identity.return_value = ControllerIdentity("controller", account_id=701)
        controller.get_pr_review_threads_strict.return_value = []
        pr = {"number": 124, "head": {"sha": "controller-head", "ref": "feature"}, "labels": []}
        decisions = [
            PRProgressDecision(ProgressRoute.ADMIT_CANDIDATE, "mergeable", CIEvidenceState.MISSING),
            PRProgressDecision(ProgressRoute.DISPATCH_CI, "no CI", CIEvidenceState.MISSING),
        ]
        with (
            patch.dict(os.environ, {"AUTO_CODER_INVALIDATION_DB": str(root / "invalidations.db")}),
            patch("src.auto_coder.pr_processor.get_dispatch_claim_store", return_value=claims),
            patch("src.auto_coder.pr_processor.select_pr_progress", side_effect=decisions),
            patch("src.auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[])),
            patch("src.auto_coder.pr_processor.get_detailed_checks_from_history"),
            patch("src.auto_coder.pr_processor.LabelManager"),
            patch("auto_coder.util.github_action.trigger_workflow_dispatch", return_value=WorkflowDispatchResult(outcome=DispatchOutcome.ACCEPTED)) as dispatch,
        ):
            actions = _handle_pr_merge(controller, "owner/repo", pr, AutomationConfig(), {})

        dispatch.assert_called_once_with("owner/repo", "ci.yml", "feature", controller)
        assert any("Triggered ci.yml" in action for action in actions)


def test_production_dispatch_uses_controller_auth_and_suppresses_uncertain_retry():
    class DispatchProvider:
        def __init__(self, token) -> None:
            self.requests = []
            self.token = token

        def credential_for(self, repo_name, required_permissions):
            self.requests.append((repo_name, dict(required_permissions)))
            return ControllerCredential(
                self.token,
                "app",
                repository=repo_name,
                permissions=tuple(sorted(required_permissions.items())),
                expires_at=datetime.now(timezone.utc),
                app_id=7,
                installation_id=11,
                snapshot_id=str(len(self.requests)),
            )

        def identity_for(self, repo_name):
            return ControllerIdentity("controller[bot]", account_id=701, app_id=7)

        def invalidate(self, credential):
            return None

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        claims = DispatchClaimStore(root / "claims.db")
        provider = DispatchProvider("controller-dispatch-token")
        controller = GitHubClient(auth_provider=provider, repository="owner/repo")
        renewed_provider = DispatchProvider("renewed-controller-token")
        restarted_controller = GitHubClient(auth_provider=renewed_provider, repository="owner/repo")
        pr = {
            "number": 125,
            "head": {"sha": "uncertain-head", "ref": "feature"},
            "labels": [],
            "body": "",
            "state": "open",
            "user": {"login": "contributor"},
        }
        decisions = [
            PRProgressDecision(ProgressRoute.ADMIT_CANDIDATE, "mergeable", CIEvidenceState.MISSING),
            PRProgressDecision(ProgressRoute.DISPATCH_CI, "no CI", CIEvidenceState.MISSING),
        ] * 2
        sends = []

        def lose_dispatch_response(method, url, **kwargs):
            sends.append((method.upper(), url, kwargs["headers"]["Authorization"]))
            request = httpx.Request(method, url, headers=kwargs["headers"])
            raise httpx.ReadError("dispatch response lost", request=request)

        transport = MagicMock()
        transport.request.side_effect = lose_dispatch_response
        with (
            patch.dict(os.environ, {"AUTO_CODER_INVALIDATION_DB": str(root / "invalidations.db")}),
            patch("src.auto_coder.pr_processor.get_dispatch_claim_store", return_value=claims) as get_claims,
            patch("src.auto_coder.pr_processor.select_pr_progress", side_effect=decisions),
            patch("src.auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[])),
            patch("src.auto_coder.pr_processor.get_detailed_checks_from_history"),
            patch("src.auto_coder.pr_processor.LabelManager"),
            patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=transport),
            patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport),
            patch.object(controller, "get_pull_request_metadata_strict", return_value=pr),
            patch.object(controller, "get_pr_review_threads_strict", return_value=[]),
            patch.object(restarted_controller, "get_pull_request_metadata_strict", return_value=pr),
            patch.object(restarted_controller, "get_pr_review_threads_strict", return_value=[]),
        ):
            first_actions = _handle_pr_merge(controller, "owner/repo", pr, AutomationConfig(), {})
            get_claims.return_value = DispatchClaimStore(root / "claims.db")
            second_actions = _handle_pr_merge(restarted_controller, "owner/repo", pr, AutomationConfig(), {})

        dispatch_sends = [request for request in sends if request[0] == "POST" and request[1].endswith("/dispatches")]
        assert dispatch_sends == [
            (
                "POST",
                "https://api.github.com/repos/owner/repo/actions/workflows/ci.yml/dispatches",
                "Bearer controller-dispatch-token",
            )
        ], (first_actions, second_actions, provider.requests)
        assert provider.requests == [("owner/repo", {"actions": "write"})]
        assert renewed_provider.requests == []
        assert any("outcome=indeterminate" in action for action in first_actions)
        assert any("dispatch already claimed" in action for action in second_actions)


def test_production_dispatch_422_repair_uses_controller_credentials_and_preserves_outcome():
    class RepairProvider:
        def __init__(self, deny_repair=False) -> None:
            self.deny_repair = deny_repair
            self.requests = []

        def credential_for(self, repo_name, required_permissions):
            permissions = dict(required_permissions)
            self.requests.append((repo_name, permissions))
            if self.deny_repair and "contents" in permissions:
                raise RuntimeError("controller repair grants unavailable")
            token = "controller-actions-token" if permissions == {"actions": "write"} else "controller-repair-token"
            return ControllerCredential(
                token,
                "app",
                repository=repo_name,
                permissions=tuple(sorted(permissions.items())),
                app_id=7,
                installation_id=11,
                snapshot_id=str(len(self.requests)),
            )

        def identity_for(self, repo_name):
            return ControllerIdentity("controller[bot]", account_id=701, app_id=7)

        def invalidate(self, credential):
            return None

    def run_case(deny_repair):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            claims = DispatchClaimStore(root / "claims.db")
            provider = RepairProvider(deny_repair)
            controller = GitHubClient(auth_provider=provider, repository="owner/repo")
            pr = {
                "number": 126 if not deny_repair else 127,
                "head": {"sha": "repair-head", "ref": "feature"},
                "labels": [],
                "body": "",
                "state": "open",
                "user": {"login": "contributor"},
            }
            decisions = [
                PRProgressDecision(ProgressRoute.ADMIT_CANDIDATE, "mergeable", CIEvidenceState.MISSING),
                PRProgressDecision(ProgressRoute.DISPATCH_CI, "no CI", CIEvidenceState.MISSING),
            ]
            sends = []

            def send(method, url, **kwargs):
                method = method.upper()
                sends.append((method, url, kwargs["headers"]["Authorization"], kwargs.get("json")))
                request = httpx.Request(method, url, headers=kwargs["headers"])
                dispatches = [item for item in sends if item[0] == "POST" and item[1].endswith("/dispatches")]
                if method == "POST" and url.endswith("/dispatches") and len(dispatches) == 1:
                    return httpx.Response(422, json={"message": "workflow_dispatch missing"}, request=request)
                if method == "GET" and "/contents/" in url:
                    content = base64.b64encode(b"name: CI\non:\n  push:\n").decode()
                    return httpx.Response(200, json={"content": content, "sha": "workflow-sha"}, request=request)
                if method == "PUT" and "/contents/" in url:
                    return httpx.Response(200, json={"content": {"sha": "updated"}}, request=request)
                return httpx.Response(204, request=request)

            transport = MagicMock()
            transport.request.side_effect = send
            with (
                patch.dict(os.environ, {"AUTO_CODER_INVALIDATION_DB": str(root / "invalidations.db")}),
                patch("src.auto_coder.pr_processor.get_dispatch_claim_store", return_value=claims),
                patch("src.auto_coder.pr_processor.select_pr_progress", side_effect=decisions),
                patch("src.auto_coder.pr_processor._check_github_actions_status", side_effect=authoritative_ci_status(success=True, ids=[])),
                patch("src.auto_coder.pr_processor.get_detailed_checks_from_history"),
                patch("src.auto_coder.pr_processor.LabelManager"),
                patch("src.auto_coder.util.gh_cache.get_caching_client", return_value=transport),
                patch("auto_coder.util.gh_cache.get_caching_client", return_value=transport),
                patch("time.sleep"),
                patch.object(controller, "get_pull_request_metadata_strict", return_value=pr),
                patch.object(controller, "get_pr_review_threads_strict", return_value=[]),
            ):
                actions = _handle_pr_merge(controller, "owner/repo", pr, AutomationConfig(), {})
            return actions, sends, provider.requests

    actions, sends, permission_requests = run_case(False)
    github_sends = [item for item in sends if item[1].endswith("/actions/workflows/ci.yml/dispatches") or item[2] == "Bearer controller-repair-token"]
    assert [(method, token) for method, _, token, _ in github_sends] == [
        ("POST", "Bearer controller-actions-token"),
        ("GET", "Bearer controller-repair-token"),
        ("PUT", "Bearer controller-repair-token"),
        ("POST", "Bearer controller-actions-token"),
    ]
    update = github_sends[2][3]
    assert update["message"] == "Auto-Coder: Add workflow_dispatch trigger to ci.yml"
    assert update["sha"] == "workflow-sha"
    assert update["branch"] == "feature"
    assert "workflow_dispatch:" in base64.b64decode(update["content"]).decode()
    assert permission_requests == [
        ("owner/repo", {"actions": "write"}),
        ("owner/repo", {"contents": "write", "workflows": "write"}),
        ("owner/repo", {"contents": "write", "workflows": "write"}),
        ("owner/repo", {"actions": "write"}),
    ]
    assert any("Triggered ci.yml" in action for action in actions)

    denied_actions, denied_sends, denied_permissions = run_case(True)
    denied_github_sends = [item for item in denied_sends if item[1].endswith("/actions/workflows/ci.yml/dispatches") or item[2] == "Bearer controller-repair-token"]
    assert [(method, token) for method, _, token, _ in denied_github_sends] == [("POST", "Bearer controller-actions-token")]
    assert denied_permissions[-1] == ("owner/repo", {"contents": "write", "workflows": "write"})
    assert not any("Triggered ci.yml" in action for action in denied_actions)
    assert any("outcome=rejected" in action for action in denied_actions)
