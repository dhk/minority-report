import json
import re
from pathlib import Path
from typing import Any, Self, cast

import pytest

from alexandria import mcp_server
from alexandria.commission import RunStore
from alexandria.commission_models import CallRecord, InputArtifact
from alexandria.infrastructure.config import ENV_DATA_DIR, ENV_REPO_ROOT, Config, load_config
from alexandria.input_resolution import extract_input
from alexandria.mcp_server import (
    _extra_allowed_hosts,
    _http_token,
    _tailscale_dns_name,
    _tunnel_path,
    _tunnel_port,
    begin_research,
    build_transport_security,
    connector_urls,
    draft_resolution,
    list_research,
    main,
    render_urls,
    run_research,
    search_research,
    show_research,
    status,
)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo_root = tmp_path / "repo"
    (repo_root / "docs").mkdir(parents=True)
    (repo_root / "docs" / "DESIGN.md").write_text("design", encoding="utf-8")
    (repo_root / "AGENTS.md").write_text("agents", encoding="utf-8")
    monkeypatch.setenv(ENV_REPO_ROOT, str(repo_root))
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "state"))
    return repo_root


def test_status_reports_not_found_without_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ENV_REPO_ROOT, raising=False)
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    assert "ALEXANDRIA_REPO" in status()


def test_status_reports_empty_research(repo: Path) -> None:
    result = status()
    assert str(repo) in result
    assert "No research investigations yet" in result


def test_status_counts_investigations_by_stage_and_assurance(repo: Path) -> None:
    investigation = repo / "research" / "2026-07-28-slug"
    (investigation / "00-topic").mkdir(parents=True)
    (investigation / "00-topic" / "topic.md").write_text("x", encoding="utf-8")
    (investigation / "topic.yaml").write_text("assurance_level: bronze\n", encoding="utf-8")
    result = status()
    assert "Investigations: 1" in result
    assert "00-topic: 1" in result
    assert "bronze: 1" in result


def test_list_research_filters_by_assurance(repo: Path) -> None:
    for slug, level in [("alpha", "bronze"), ("beta", "gold")]:
        investigation = repo / "research" / slug
        investigation.mkdir(parents=True)
        (investigation / "topic.yaml").write_text(
            f"title: {slug.title()}\nassurance_level: {level}\n", encoding="utf-8"
        )
    result = list_research(assurance="gold")
    assert "beta" in result
    assert "alpha" not in result


def test_show_research_reports_missing_slug(repo: Path) -> None:
    assert "No investigation" in show_research("nope")


def test_show_research_includes_readme(repo: Path) -> None:
    investigation = repo / "research" / "alpha"
    investigation.mkdir(parents=True)
    (investigation / "topic.yaml").write_text("title: Alpha\n", encoding="utf-8")
    (investigation / "README.md").write_text("Alpha investigation notes.", encoding="utf-8")
    result = show_research("alpha")
    assert "Alpha investigation notes." in result


def test_search_research_reports_matches(repo: Path) -> None:
    investigation = repo / "research" / "alpha"
    investigation.mkdir(parents=True)
    (investigation / "README.md").write_text("mentions warp drives here", encoding="utf-8")
    result = search_research("warp drives")
    assert "alpha/README.md" in result


def test_search_research_reports_no_matches(repo: Path) -> None:
    assert "No matches" in search_research("nonexistent-term")


def test_http_token_generated_and_stable(tmp_path: Path) -> None:
    config = Config(
        data_dir=tmp_path, data_dir_source="test", repo_root=tmp_path, repo_root_source="test"
    )
    first = _http_token(config)
    second = _http_token(config)
    assert first == second
    assert oct((tmp_path / "mcp-http-token").stat().st_mode)[-3:] == "600"


def test_extra_allowed_hosts_merges_cli_and_env() -> None:
    hosts = _extra_allowed_hosts(
        ["a.example"],
        env={"ALEXANDRIA_ALLOWED_HOSTS": "b.example"},
        tailscale=lambda: "lobster.tail.ts.net",
    )
    assert hosts == ["a.example", "b.example", "lobster.tail.ts.net"]


def test_no_config_and_no_tailscale_means_no_extra_hosts() -> None:
    assert _extra_allowed_hosts(None, env={}, tailscale=lambda: None) == []


def test_tailscale_dns_detection_is_best_effort() -> None:
    class Result:
        stdout = '{"Self":{"DNSName":"lobster.tail.ts.net."}}'

    assert _tailscale_dns_name(lambda *_args, **_kwargs: Result()) == "lobster.tail.ts.net"

    def missing(*_args: object, **_kwargs: object) -> object:
        raise FileNotFoundError("tailscale")

    assert _tailscale_dns_name(missing) is None
    assert _tailscale_dns_name(lambda *_args, **_kwargs: type("R", (), {"stdout": "bad"})()) is None


def test_tunnel_path_and_port_resolve_cli_then_environment() -> None:
    environment = {
        "ALEXANDRIA_TUNNEL_PATH": "from-env",
        "ALEXANDRIA_TUNNEL_PORT": "8443",
    }
    assert _tunnel_path(None, environment) == "/from-env"
    assert _tunnel_path("/from-cli/", environment) == "/from-cli"
    assert _tunnel_port(None, environment) == 8443
    assert _tunnel_port(10000, environment) == 10000
    assert _tunnel_port(None, {"ALEXANDRIA_TUNNEL_PORT": "invalid"}) is None


def test_connector_urls_include_token() -> None:
    pairs = connector_urls("tok123", [], port=9000)
    assert dict(pairs)["MCP over HTTP"] == "http://127.0.0.1:9000/mcp/tok123"

    tunneled = dict(
        connector_urls(
            "tok123",
            ["lobster.tail.ts.net"],
            tunnel_path="/alexandria",
            tunnel_port=8443,
        )
    )
    assert tunneled["Tunnel MCP connector"] == (
        "https://lobster.tail.ts.net:8443/alexandria/mcp/tok123"
    )


def test_render_urls_formats_lines() -> None:
    assert render_urls("tok123", []) == ["MCP over HTTP: http://127.0.0.1:8797/mcp/tok123"]


@pytest.mark.anyio
async def test_health_response_identifies_alexandria() -> None:
    response = await mcp_server._health(cast(Any, None))
    payload = json.loads(bytes(response.body))

    assert payload["service"] == "alexandria"
    assert payload["version"]
    assert payload["started_at"]


def test_transport_security_always_includes_loopback() -> None:
    settings = build_transport_security(["tunnel.example"])
    assert "127.0.0.1:*" in settings.allowed_hosts
    assert settings.enable_dns_rebinding_protection is True


def test_http_mode_exits_cleanly_when_repo_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A missing/undetectable repo must be a clean exit(1) with a stderr
    message, never an uncaught RepoNotFoundError traceback — that's the
    difference between a client seeing "server error" on every reconnect
    and seeing why, once (regression test for the bug this was).
    """
    monkeypatch.delenv(ENV_REPO_ROOT, raising=False)
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "state"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc_info:
        main(["--http"])
    assert exc_info.value.code == 1
    assert "ALEXANDRIA_REPO" in capsys.readouterr().err


class FakeCommissionGateway:
    def __init__(self, api_key: str) -> None:
        assert api_key == "test-key"

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def estimate(
        self, models: list[str], input_tokens: int, *, web_search: bool = False
    ) -> float:
        assert models
        assert input_tokens > 0
        return 0.10

    async def complete(self, model: str, prompt: str, *, web_search: bool = False) -> CallRecord:
        if model == "grader/model":
            body = json.dumps(
                {
                    "claims": [
                        {
                            "text": "The interface should remain narrow.",
                            "scores": [
                                {"model_index": 1, "score": 3, "quote": "remain narrow"},
                                {"model_index": 2, "score": 0, "quote": ""},
                            ],
                        }
                    ],
                    "report_markdown": "# Report\n\n## What this run does not establish\n\nTruth.",
                }
            )
        else:
            body = "Evidence says the interface should remain narrow."
        return CallRecord(
            model_id=model,
            status="success",
            body=body,
            raw_response=json.dumps({"model": model, "body": body}),
            generation_id="gen-" + model.replace("/", "-"),
            cost=0.01,
            latency_ms=10,
        )


@pytest.mark.anyio
async def test_begin_research_from_paste_requires_separate_confirmation(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mcp_server, "openrouter_api_key", lambda: "test-key")
    monkeypatch.setattr(mcp_server, "OpenRouterGateway", FakeCommissionGateway)

    review = await begin_research(
        task="Assess the interface.",
        pasted_content="Keep the interface narrow.",
        models=["alpha/model", "beta/model"],
        grading_model="grader/model",
        ceiling_usd=1.0,
    )

    assert "no provider model calls have been dispatched" in review
    assert "pasted-content.md" in review
    draft_id = re.search(r"Draft: (d-[a-f0-9]+)", review)
    assert draft_id is not None
    state = RunStore(load_config().data_dir)
    assert state.list_runs() == []

    blocked = await run_research(draft_id.group(1))
    assert "Dispatch blocked" in blocked
    assert state.list_runs() == []

    result = await run_research(draft_id.group(1), f"RUN {draft_id.group(1)}")
    assert "Research run finished" in result
    assert len(state.list_runs()) == 1


@pytest.mark.anyio
async def test_begin_research_resolves_supported_url(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class FakeResolver:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def resolve(self, url: str) -> list[InputArtifact]:
            assert url == "https://github.com/dhk/alexandria/issues/3"
            return [extract_input("issue-3.md", b"Compare the options.", source_url=url)]

    monkeypatch.setattr(mcp_server, "openrouter_api_key", lambda: "test-key")
    monkeypatch.setattr(mcp_server, "OpenRouterGateway", FakeCommissionGateway)
    monkeypatch.setattr(mcp_server, "GitHubResolver", FakeResolver)

    review = await begin_research(
        task="Compare the options.",
        url="https://github.com/dhk/alexandria/issues/3",
        models=["alpha/model", "beta/model"],
        grading_model="grader/model",
    )

    assert "issue-3.md" in review
    assert "no provider model calls have been dispatched" in review


@pytest.mark.anyio
async def test_begin_research_requires_paste_or_url(repo: Path) -> None:
    result = await begin_research(task="Research this.")
    assert "Provide pasted content" in result


def _make_investigation(repo: Path, slug: str = "alpha") -> Path:
    investigation = repo / "research" / slug
    investigation.mkdir(parents=True)
    (investigation / "topic.yaml").write_text(f"title: {slug.title()}\n", encoding="utf-8")
    return investigation


def test_draft_resolution_reports_unknown_slug(repo: Path) -> None:
    result = draft_resolution(slug="nope", outcome="implemented")
    assert "No investigation" in result


def test_draft_resolution_validates_and_does_not_write(repo: Path) -> None:
    investigation = _make_investigation(repo)
    result = draft_resolution(slug="alpha", outcome="implemented")
    assert "validates" in result
    assert "outcome: implemented" in result
    # Same discipline as run_research: this tool drafts, it never writes.
    assert not (investigation / "resolution.yaml").exists()


def test_draft_resolution_rejects_morphed_without_expression(repo: Path) -> None:
    _make_investigation(repo)
    result = draft_resolution(slug="alpha", outcome="morphed")
    assert "not valid" in result
    assert "expression" in result


def test_draft_resolution_accepts_morphed_with_expression(repo: Path) -> None:
    _make_investigation(repo)
    result = draft_resolution(slug="alpha", outcome="morphed", expression="research/successor-idea")
    assert "validates" in result
    assert "expression: research/successor-idea" in result
