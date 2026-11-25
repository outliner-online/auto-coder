"""Native complete PR conversation reads used by delivery-receipt recovery."""

from __future__ import annotations

import httpx
import pytest

from auto_coder.util import gh_cache
from auto_coder.util.gh_cache import GitHubClient


def _comment(index: int, body: str = "discussion") -> dict:
    return {"id": index, "body": body, "created_at": "2026-11-25T00:00:00Z", "user": {"id": 10, "login": "prior-controller"}}


def test_native_pr_conversation_reads_every_page_with_author_ids(monkeypatch):
    pages = {1: [_comment(i) for i in range(1, 101)], 2: [_comment(101, "later receipt")]}
    seen = []

    def wire(_transport, request):
        page = int(request.url.params["page"])
        seen.append(page)
        assert request.headers["Cache-Control"] == "no-cache"
        return httpx.Response(200, json=pages[page], request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", wire)
    monkeypatch.setattr(gh_cache, "get_caching_client", lambda *args, **kwargs: pytest.fail("strict conversation must bypass the HTTP cache"))
    comments = GitHubClient("controller-token").get_pr_comments_strict("owner/repo", 7)
    assert seen == [1, 2]
    assert len(comments) == 101
    assert comments[-1]["body"] == "later receipt"
    assert comments[-1]["user"] == {"id": 10, "login": "prior-controller"}


def test_native_empty_conversation_is_complete(monkeypatch):
    monkeypatch.setattr(gh_cache, "_strict_request", lambda method, url, **kwargs: httpx.Response(200, json=[], request=httpx.Request(method, url)))
    assert GitHubClient("controller-token").get_pr_comments_strict("owner/repo", 7) == []


@pytest.mark.parametrize("failed_page", [1, 2])
def test_native_conversation_never_returns_a_partial_scan(monkeypatch, failed_page):
    def response(method, url, **kwargs):
        page = kwargs["params"]["page"]
        return httpx.Response(503 if page == failed_page else 200, json=[] if page == failed_page else [_comment(i) for i in range(1, 101)], request=httpx.Request(method, url))

    monkeypatch.setattr(gh_cache, "_strict_request", response)
    with pytest.raises(httpx.HTTPStatusError):
        GitHubClient("controller-token").get_pr_comments_strict("owner/repo", 7)


@pytest.mark.parametrize("payload", [None, {}, [None], [{"id": True, "body": "text"}], [{"id": 1, "body": None}], [{"id": 1, "body": "text", "user": {"login": "prior", "id": 0}}]])
def test_native_conversation_rejects_invalid_scans(monkeypatch, payload):
    monkeypatch.setattr(gh_cache, "_strict_request", lambda method, url, **kwargs: httpx.Response(200, content=b"null" if payload is None else __import__("json").dumps(payload).encode(), request=httpx.Request(method, url)))
    with pytest.raises(RuntimeError, match="Native PR conversation"):
        GitHubClient("controller-token").get_pr_comments_strict("owner/repo", 7)


def test_native_conversation_preserves_deleted_authors_as_unavailable_evidence(monkeypatch):
    comment = _comment(1)
    comment["user"] = None
    monkeypatch.setattr(gh_cache, "_strict_request", lambda method, url, **kwargs: httpx.Response(200, json=[comment], request=httpx.Request(method, url)))
    assert GitHubClient("controller-token").get_pr_comments_strict("owner/repo", 7)[0]["user"] is None


def test_native_conversation_duplicate_page_is_unavailable(monkeypatch):
    monkeypatch.setattr(gh_cache, "_strict_request", lambda method, url, **kwargs: httpx.Response(200, json=[_comment(i) for i in range(1, 101)], request=httpx.Request(method, url)))
    with pytest.raises(RuntimeError, match="repeated"):
        GitHubClient("controller-token").get_pr_comments_strict("owner/repo", 7)


def test_native_conversation_truncation_is_unavailable(monkeypatch):
    monkeypatch.setattr(gh_cache, "COMMENTS_MAX_PAGES", 1)
    monkeypatch.setattr(gh_cache, "_strict_request", lambda method, url, **kwargs: httpx.Response(200, json=[_comment(i) for i in range(1, 101)], request=httpx.Request(method, url)))
    with pytest.raises(RuntimeError, match="incomplete after 1 pages"):
        GitHubClient("controller-token").get_pr_comments_strict("owner/repo", 7)


def test_native_conversation_inconsistent_next_page_is_unavailable(monkeypatch):
    monkeypatch.setattr(gh_cache, "_strict_request", lambda method, url, **kwargs: httpx.Response(200, json=[_comment(1)], headers={"Link": f'<{url}?page=1&per_page=100>; rel="next"'}, request=httpx.Request(method, url)))
    with pytest.raises(RuntimeError, match="pagination"):
        GitHubClient("controller-token").get_pr_comments_strict("owner/repo", 7)
