"""Tests for tools/mark.py — GitHub API job status marker."""

import base64
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mark  # noqa: E402


def encode_content(obj):
    """Encode JSON to base64 as GitHub API returns."""
    return base64.b64encode((json.dumps(obj, indent=1, sort_keys=True) + "\n").encode()).decode()


class MockHTTP:
    """Mock HTTP responses for GitHub API."""

    def __init__(self):
        self.requests = []
        self.responses = {}  # (method, path) -> (status, body_dict)
        self.put_sha_counter = 0

    def add_response(self, method, path, status, body):
        self.responses[(method, path)] = (status, body)

    def add_file(self, path, content, sha="abc123"):
        """Add a file response."""
        body = {"content": encode_content(content), "sha": sha}
        self.responses[("GET", path)] = (200, body)

    def add_put_success(self, path, new_sha=None):
        """Add a successful PUT response."""
        self.put_sha_counter += 1
        sha = new_sha or f"newsha{self.put_sha_counter}"
        body = {"content": {"sha": sha}}
        self.responses[("PUT", path)] = (201, body)

    def urlopen(self, req):
        method = req.method
        url = req.full_url
        path = url.replace("https://api.github.com/repos/", "")
        path = path.split("?")[0]

        self.requests.append({"method": method, "url": url, "path": path,
                              "data": json.loads(req.data.decode()) if req.data else None})

        key = (method, path)
        if key not in self.responses:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        status, body = self.responses[key]
        if status >= 400:
            import io
            raise urllib.error.HTTPError(url, status, f"HTTP {status}", {}, io.BytesIO(json.dumps(body).encode()))

        return MockResponse(status, body)


class MockResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return json.dumps(self._body).encode()


@pytest.fixture
def mock_http(monkeypatch):
    http = MockHTTP()
    monkeypatch.setattr(urllib.request, "urlopen", http.urlopen)
    return http


@pytest.fixture
def gh_env(monkeypatch):
    monkeypatch.setenv("JOBWATCH_GH_TOKEN", "test_token_123")
    monkeypatch.setenv("JOBWATCH_REPO", "gaivic/jobwatch")


class TestNorm:
    def test_lowercase_and_strip(self):
        assert mark.norm("  TikTok  ") == "tiktok"

    def test_removes_special_chars(self):
        assert mark.norm("Jane St.") == "jane st"
        assert mark.norm("T-Mobile") == "t mobile"

    def test_normalizes_whitespace(self):
        assert mark.norm("Foo   Bar  Baz") == "foo bar baz"


class TestStageRank:
    def test_is_backward(self):
        assert mark.is_backward("interview", "applied")
        assert mark.is_backward("offer", "seen")
        assert not mark.is_backward("applied", "interview")
        assert not mark.is_backward("seen", "applied")
        assert not mark.is_backward("applied", "applied")

    def test_queued_to_applied_is_forward(self):
        assert not mark.is_backward("queued", "applied")
        assert mark.is_backward("applied", "queued")

    def test_queued_rank(self):
        assert mark.STAGE_RANK["queued"] < mark.STAGE_RANK["applied"]
        assert mark.STAGE_RANK["queued"] > mark.STAGE_RANK["seen"]

    def test_current_stage_none(self):
        assert mark.current_stage({}, "job1") == "todo"

    def test_current_stage_string(self):
        assert mark.current_stage({"job1": "applied"}, "job1") == "applied"

    def test_current_stage_dict(self):
        assert mark.current_stage({"job1": {"s": "interview"}}, "job1") == "interview"

    def test_current_stage_queued(self):
        assert mark.current_stage({"job1": {"s": "queued"}}, "job1") == "queued"


class TestFindJobs:
    def test_match_by_company(self):
        roles = [
            {"id": "1", "company": "TikTok", "title": "SWE"},
            {"id": "2", "company": "Google", "title": "SWE"},
        ]
        matches = mark.find_jobs(roles, "tiktok", None, None)
        assert len(matches) == 1
        assert matches[0]["id"] == "1"

    def test_match_by_company_and_title(self):
        roles = [
            {"id": "1", "company": "TikTok", "title": "Software Engineer"},
            {"id": "2", "company": "TikTok", "title": "Data Scientist"},
        ]
        matches = mark.find_jobs(roles, "tiktok", "software", None)
        assert len(matches) == 1
        assert matches[0]["id"] == "1"

    def test_partial_match(self):
        roles = [
            {"id": "1", "company": "TikTok Inc.", "title": "Software Engineer New Grad"},
        ]
        matches = mark.find_jobs(roles, "tiktok", "engineer", None)
        assert len(matches) == 1

    def test_no_match(self):
        roles = [{"id": "1", "company": "TikTok", "title": "SWE"}]
        matches = mark.find_jobs(roles, "google", None, None)
        assert len(matches) == 0

    def test_match_by_url(self):
        roles = [
            {"id": "1", "company": "TikTok", "title": "SWE", "url": "https://jobs.tiktok.com/123"},
            {"id": "2", "company": "TikTok", "title": "SWE", "url": "https://jobs.tiktok.com/456"},
        ]
        matches = mark.find_jobs(roles, "tiktok", None, "123")
        assert len(matches) == 1
        assert matches[0]["id"] == "1"


class TestFilterActive:
    def test_filters_inactive(self):
        roles = [
            {"id": "1", "active": True},
            {"id": "2", "active": False},
            {"id": "3"},  # no active field
        ]
        active = mark.filter_active(roles)
        assert len(active) == 2
        assert [r["id"] for r in active] == ["1", "3"]


class TestBuildEntry:
    def test_new_entry(self):
        entry = mark.build_entry(None, "applied", 1000)
        assert entry["s"] == "applied"
        assert entry["t"] == 1000
        assert "h" in entry

    def test_updates_history(self):
        prev = {"s": "seen", "t": 900}
        entry = mark.build_entry(prev, "applied", 1000)
        assert entry["s"] == "applied"
        assert len(entry["h"]) == 2
        assert entry["h"][-1]["s"] == "applied"

    def test_preserves_star_and_note(self):
        prev = {"s": "seen", "star": True, "note": "test note"}
        entry = mark.build_entry(prev, "applied", 1000)
        assert entry["star"] is True
        assert entry["note"] == "test note"

    def test_todo_clears_entry(self):
        entry = mark.build_entry(None, "todo", 1000)
        assert entry is None

    def test_todo_preserves_star(self):
        prev = {"s": "seen", "star": True, "note": "keep"}
        entry = mark.build_entry(prev, "todo", 1000)
        assert entry == {"star": True, "note": "keep"}


class TestCreateManualRole:
    def test_creates_role_structure(self):
        role = mark.create_manual_role("TestCo", "Engineer", "http://example.com", "NYC", "SWE")
        assert role["company"] == "TestCo"
        assert role["title"] == "Engineer"
        assert role["url"] == "http://example.com"
        assert role["location"] == "NYC"
        assert role["category"] == "SWE"
        assert role["manual"] is True
        assert role["active"] is True
        assert role["id"].startswith("manual:")

    def test_defaults_for_optional_fields(self):
        role = mark.create_manual_role("TestCo", "Engineer", None, None, None)
        assert role["url"] == ""
        assert role["location"] == "location N/A"
        assert role["category"] == "SWE"


class TestCLIFind(object):
    def test_find_returns_matches(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/roles.json", [
            {"id": "1", "company": "TikTok", "title": "SWE", "url": "http://example.com", "location": "CA", "active": True},
            {"id": "2", "company": "Google", "title": "SWE", "url": "http://example.com", "location": "CA", "active": True},
        ])
        mock_http.add_file("gaivic/jobwatch/contents/manual.json", [])
        mock_http.add_file("gaivic/jobwatch/contents/status.json", {})

        exit_code = mark.main(["find", "--company", "tiktok"])
        assert exit_code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True
        assert output["count"] == 1
        assert output["matches"][0]["company"] == "TikTok"

    def test_find_with_stage_filter(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/roles.json", [
            {"id": "1", "company": "TikTok", "title": "SWE", "url": "http://example.com", "location": "CA", "active": True},
            {"id": "2", "company": "Google", "title": "SWE", "url": "http://example.com", "location": "CA", "active": True},
            {"id": "3", "company": "Meta", "title": "SWE", "url": "http://example.com", "location": "CA", "active": True},
        ])
        mock_http.add_file("gaivic/jobwatch/contents/manual.json", [])
        mock_http.add_file("gaivic/jobwatch/contents/status.json", {
            "1": {"s": "queued", "t": 1000},
            "2": {"s": "applied", "t": 1000},
        })

        exit_code = mark.main(["list", "--stage", "queued"])
        assert exit_code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True
        assert output["count"] == 1
        assert output["matches"][0]["id"] == "1"
        assert output["matches"][0]["current_stage"] == "queued"

    def test_find_no_token(self, monkeypatch, capsys):
        monkeypatch.delenv("JOBWATCH_GH_TOKEN", raising=False)
        exit_code = mark.main(["find", "--company", "tiktok"])
        assert exit_code == 1
        output = json.loads(capsys.readouterr().out)
        assert "JOBWATCH_GH_TOKEN" in output["error"]


class TestCLIMark:
    def test_mark_by_id(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/status.json", {}, sha="sha1")
        mock_http.add_put_success("gaivic/jobwatch/contents/status.json")

        exit_code = mark.main(["applied", "--id", "simplify:abc123"])
        assert exit_code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True
        assert output["new_stage"] == "applied"
        assert output["job_id"] == "simplify:abc123"

    def test_mark_by_company(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/roles.json", [
            {"id": "simplify:abc", "company": "TikTok", "title": "SWE", "active": True},
        ])
        mock_http.add_file("gaivic/jobwatch/contents/manual.json", [])
        mock_http.add_file("gaivic/jobwatch/contents/status.json", {}, sha="sha1")
        mock_http.add_put_success("gaivic/jobwatch/contents/status.json")

        exit_code = mark.main(["applied", "--company", "tiktok"])
        assert exit_code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True
        assert output["job_id"] == "simplify:abc"

    def test_mark_multiple_matches_fails(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/roles.json", [
            {"id": "1", "company": "TikTok", "title": "SWE", "active": True},
            {"id": "2", "company": "TikTok", "title": "Data", "active": True},
        ])
        mock_http.add_file("gaivic/jobwatch/contents/manual.json", [])

        exit_code = mark.main(["applied", "--company", "tiktok"])
        assert exit_code == 1

        output = json.loads(capsys.readouterr().out)
        assert "Multiple matches" in output["error"]
        assert len(output["candidates"]) == 2

    def test_mark_no_match_fails(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/roles.json", [])
        mock_http.add_file("gaivic/jobwatch/contents/manual.json", [])

        exit_code = mark.main(["applied", "--company", "nonexistent"])
        assert exit_code == 1

        output = json.loads(capsys.readouterr().out)
        assert "No jobs match" in output["error"]

    def test_backward_movement_blocked(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/status.json",
                          {"simplify:abc": {"s": "interview", "t": 1000}}, sha="sha1")

        exit_code = mark.main(["applied", "--id", "simplify:abc"])
        assert exit_code == 1

        output = json.loads(capsys.readouterr().out)
        assert "backward" in output["error"].lower()

    def test_backward_with_force(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/status.json",
                          {"simplify:abc": {"s": "interview", "t": 1000}}, sha="sha1")
        mock_http.add_put_success("gaivic/jobwatch/contents/status.json")

        exit_code = mark.main(["applied", "--id", "simplify:abc", "--force"])
        assert exit_code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True

    def test_dry_run(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/status.json", {}, sha="sha1")

        exit_code = mark.main(["applied", "--id", "simplify:abc", "--dry-run"])
        assert exit_code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["action"] == "dry_run"
        assert len([r for r in mock_http.requests if r["method"] == "PUT"]) == 0

    def test_add_manual(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/roles.json", [])
        mock_http.add_file("gaivic/jobwatch/contents/manual.json", [], sha="manual_sha")
        mock_http.add_file("gaivic/jobwatch/contents/status.json", {}, sha="status_sha")
        mock_http.add_put_success("gaivic/jobwatch/contents/manual.json")
        mock_http.add_put_success("gaivic/jobwatch/contents/status.json")

        exit_code = mark.main(["applied", "--company", "NewCo", "--title", "Engineer", "--add-manual"])
        assert exit_code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True
        assert output["job_id"].startswith("manual:")

    def test_add_manual_requires_title(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/roles.json", [])
        mock_http.add_file("gaivic/jobwatch/contents/manual.json", [])

        exit_code = mark.main(["applied", "--company", "NewCo", "--add-manual"])
        assert exit_code == 1

        output = json.loads(capsys.readouterr().out)
        assert "--title required" in output["error"]


class TestCLIStageShortcuts:
    def test_stage_shortcut(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/status.json", {}, sha="sha1")
        mock_http.add_put_success("gaivic/jobwatch/contents/status.json")

        for stage in ("seen", "queued", "applied", "oa", "interview", "offer", "rejected", "dropped"):
            exit_code = mark.main([stage, "--id", f"job_{stage}"])
            assert exit_code == 0


class TestQueuedStage:
    def test_mark_queued(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/status.json", {}, sha="sha1")
        mock_http.add_put_success("gaivic/jobwatch/contents/status.json")

        exit_code = mark.main(["queued", "--id", "simplify:abc123"])
        assert exit_code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True
        assert output["new_stage"] == "queued"

    def test_queued_to_applied_forward(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/status.json",
                          {"simplify:abc": {"s": "queued", "t": 1000}}, sha="sha1")
        mock_http.add_put_success("gaivic/jobwatch/contents/status.json")

        exit_code = mark.main(["applied", "--id", "simplify:abc"])
        assert exit_code == 0

        output = json.loads(capsys.readouterr().out)
        assert output["success"] is True
        assert output["previous_stage"] == "queued"
        assert output["new_stage"] == "applied"

    def test_applied_to_queued_backward_blocked(self, mock_http, gh_env, capsys):
        mock_http.add_file("gaivic/jobwatch/contents/status.json",
                          {"simplify:abc": {"s": "applied", "t": 1000}}, sha="sha1")

        exit_code = mark.main(["queued", "--id", "simplify:abc"])
        assert exit_code == 1

        output = json.loads(capsys.readouterr().out)
        assert "backward" in output["error"].lower()


class TestNoToken:
    def test_mark_no_token(self, monkeypatch, capsys):
        monkeypatch.delenv("JOBWATCH_GH_TOKEN", raising=False)
        exit_code = mark.main(["applied", "--id", "job1"])
        assert exit_code == 1
        output = json.loads(capsys.readouterr().out)
        assert "JOBWATCH_GH_TOKEN" in output["error"]
