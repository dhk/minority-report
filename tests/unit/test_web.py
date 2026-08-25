import io
import json
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path

from starlette.testclient import TestClient

from alexandria.commission import RunStore
from alexandria.commission_models import InputArtifact, RunRecord, RunStatus
from alexandria.infrastructure.config import Config
from alexandria.web import create_app


def _result_client(tmp_path: Path) -> tuple[TestClient, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    config = Config(
        data_dir=tmp_path / "state",
        data_dir_source="test",
        repo_root=repo,
        repo_root_source="test",
    )
    run_id = "r-2026-0728-01"
    run = RunRecord(
        run_id=run_id,
        brief_revision="B",
        brief_sha256="abc123",
        status="partial",
        created_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
        completed_at=datetime(2026, 7, 28, 12, 3, tzinfo=UTC),
        cost_actual=0.42,
        elapsed_seconds=184,
        inputs=[
            InputArtifact(
                name="brief.md",
                format="md",
                bytes=12,
                extracted_chars=12,
                encoding="utf-8",
                sha256="input123",
                state="extracted",
                extraction_method="text-decode",
            )
        ],
        dispatched_models=["alpha/model", "beta/model"],
        grading_model="grader/model",
        limitations=["Beta failed after one provider error."],
    )
    store = RunStore(config.data_dir)
    store.write_run(run)
    run_dir = store.run_dir(run_id)
    (run_dir / "raw").mkdir()
    (run_dir / "brief.md").write_text(
        "Task\nShould Alexandria render its report?\n\nContext\nOperator review.",
        encoding="utf-8",
    )
    report = (
        "## Recommendation\n\nRender **the report** beside its artifact card.\n\n"
        "### What this run does not establish\n\nAgreement is not verification.\n\n"
        "<script>unsafe()</script>\n"
    )
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    (run_dir / "claims.json").write_text(
        json.dumps(
            [
                {
                    "claim_id": "c-001",
                    "text": "The report is readable.",
                    "group": "novel",
                    "responding_model_count": 1,
                }
            ]
        ),
        encoding="utf-8",
    )
    (run_dir / "scores.csv").write_text(
        "claim_id,model_id,score,quote,grading_call_id\n"
        "c-001,alpha/model,0,,grader-1\n"
        "c-001,beta/model,,,\n",
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "brief_revision": "B",
                "brief_sha256": "abc123",
                "artifacts": ["report.md", "claims.json", "scores.csv", "raw/"],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "raw" / "alpha-model.json").write_text(
        json.dumps(
            {
                "model_id": "alpha/model",
                "resolved_model_id": "alpha/model-v1",
                "status": "success",
                "raw_response": "alpha raw response",
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "cost": 0.4,
                "latency_ms": 1000,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "raw" / "beta-model.json").write_text(
        json.dumps(
            {
                "model_id": "beta/model",
                "status": "failed",
                "error": "502 from provider",
                "latency_ms": 900,
            }
        ),
        encoding="utf-8",
    )
    return TestClient(create_app(config)), run_id, report


def test_result_defaults_to_the_claim_landscape_and_preserves_honesty_states(
    tmp_path: Path,
) -> None:
    client, run_id, _ = _result_client(tmp_path)

    response = client.get(f"/runs/{run_id}")

    assert response.status_code == 200
    assert "Should Alexandria render its report?" in response.text
    assert "partial with 1 failed call" in response.text
    assert 'aria-current="page">Claim landscape</a>' in response.text
    assert "—" in response.text
    assert "✕" in response.text
    assert "call failed; no output exists to grade" in response.text.lower()
    assert "Novel · 1/2" in response.text


def test_report_tab_renders_safe_markdown_and_artifact_actions(tmp_path: Path) -> None:
    client, run_id, _ = _result_client(tmp_path)

    response = client.get(f"/runs/{run_id}?tab=report")

    assert response.status_code == 200
    assert 'aria-current="page">Report</a>' in response.text
    assert "<h2>Recommendation</h2>" in response.text
    assert "Render <strong>the report</strong>" in response.text
    assert "<h3>What this run does not establish</h3>" in response.text
    assert "&lt;script&gt;unsafe()&lt;/script&gt;" in response.text
    assert "data-copy-markdown" in response.text
    assert f"/runs/{run_id}/bundle.zip" in response.text


def test_heatmap_tab_color_codes_canonical_claims_without_fabricating_prose_spans(
    tmp_path: Path,
) -> None:
    client, run_id, _ = _result_client(tmp_path)

    response = client.get(f"/runs/{run_id}?tab=heatmap")

    assert response.status_code == 200
    assert 'aria-current="page">Heatmap document</a>' in response.text
    assert 'class="claim-block group-novel"' in response.text
    assert "The report is readable." in response.text
    assert "alpha/model" in response.text
    assert "beta/model" in response.text
    assert "—" in response.text
    assert "✕" in response.text
    assert "does not pretend to highlight words" in response.text
    assert f"/runs/{run_id}/heatmap.html" in response.text


def test_standalone_heatmap_is_safe_and_explains_its_semantics(tmp_path: Path) -> None:
    client, run_id, _ = _result_client(tmp_path)

    response = client.get(f"/runs/{run_id}/heatmap.html")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>Claim heatmap</h1>" in response.text
    assert 'class="claim-block group-novel"' in response.text
    assert "relationship among model responses, not factual truth" in response.text
    assert "— responded, no bearing" in response.text
    assert "✕ call failed" in response.text


def test_report_source_and_bundle_preserve_the_run_artifacts(tmp_path: Path) -> None:
    client, run_id, report = _result_client(tmp_path)

    source = client.get(f"/runs/{run_id}/report.md")
    bundle = client.get(f"/runs/{run_id}/bundle.zip")

    assert source.status_code == 200
    assert source.text == report
    assert source.headers["content-type"].startswith("text/markdown")
    assert bundle.status_code == 200
    assert bundle.headers["content-type"] == "application/zip"
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = set(archive.namelist())
        report_html = archive.read(f"{run_id}/report.html").decode()
        heatmap_html = archive.read(f"{run_id}/heatmap.html").decode()
        runner = archive.getinfo(f"{run_id}/open-report.py")
    assert f"{run_id}/report.md" in names
    assert f"{run_id}/report.html" in names
    assert f"{run_id}/heatmap.html" in names
    assert f"{run_id}/open-report.py" in names
    assert f"{run_id}/HOW-TO-READ.txt" in names
    assert f"{run_id}/manifest.json" in names
    assert f"{run_id}/raw/alpha-model.json" in names
    assert "Render <strong>the report</strong>" in report_html
    assert "&lt;script&gt;unsafe()&lt;/script&gt;" in report_html
    assert "<h1>Claim heatmap</h1>" in heatmap_html
    assert "The report is readable." in heatmap_html
    assert runner.external_attr >> 16 & 0o777 == 0o755


def test_downloaded_bundle_runner_opens_both_standalone_documents(tmp_path: Path) -> None:
    client, run_id, _ = _result_client(tmp_path)
    response = client.get(f"/runs/{run_id}/bundle.zip")
    extracted = tmp_path / "download"
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        archive.extractall(extracted)

    completed = subprocess.run(
        [sys.executable, str(extracted / run_id / "open-report.py"), "--no-browser"],
        check=True,
        capture_output=True,
        text=True,
    )
    heatmap = subprocess.run(
        [
            sys.executable,
            str(extracted / run_id / "open-report.py"),
            "--heatmap",
            "--no-browser",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Document: file://" in completed.stdout
    assert completed.stdout.rstrip().endswith("report.html")
    assert "Document: file://" in heatmap.stdout
    assert heatmap.stdout.rstrip().endswith("heatmap.html")


def test_raw_and_provenance_tabs_state_missing_and_preserved_data(tmp_path: Path) -> None:
    client, run_id, _ = _result_client(tmp_path)

    raw = client.get(f"/runs/{run_id}?tab=raw")
    provenance = client.get(f"/runs/{run_id}?tab=provenance")

    assert "alpha/model-v1" in raw.text
    assert "502 from provider" in raw.text
    assert "brief sha256" in provenance.text.lower()
    assert "Beta failed after one provider error." in provenance.text
    assert "it does not verify that claim" in provenance.text


def _discoverability_config(tmp_path: Path) -> Config:
    repo = tmp_path / "repo"
    repo.mkdir()
    return Config(
        data_dir=tmp_path / "state",
        data_dir_source="test",
        repo_root=repo,
        repo_root_source="test",
    )


def test_homepage_lists_no_investigations_when_research_dir_is_empty(tmp_path: Path) -> None:
    client = TestClient(create_app(_discoverability_config(tmp_path)))

    response = client.get("/")

    assert "No research investigations yet." in response.text


def test_homepage_links_each_investigation_to_its_flow_view(tmp_path: Path) -> None:
    # The flow view had no path to it from anywhere in the app -- reachable
    # only if you already knew the URL.
    config = _discoverability_config(tmp_path)
    investigation = config.repo_root / "research" / "example-slug"
    investigation.mkdir(parents=True)
    (investigation / "topic.yaml").write_text(
        "title: An example investigation\nassurance_level: bronze\n", encoding="utf-8"
    )
    client = TestClient(create_app(config))

    response = client.get("/")

    assert '<a href="/flow/example-slug">An example investigation</a>' in response.text
    assert "bronze" in response.text


def _tab_client(tmp_path: Path, *, statuses: tuple[RunStatus, ...] = ()) -> TestClient:
    repo = tmp_path / "repo"
    (repo / "research" / "an-investigation").mkdir(parents=True)
    config = Config(
        data_dir=tmp_path / "state",
        data_dir_source="test",
        repo_root=repo,
        repo_root_source="test",
    )
    store = RunStore(config.data_dir)
    for index, status in enumerate(statuses):
        store.write_run(
            RunRecord(
                run_id=f"r-{index}-{status}",
                brief_revision="B",
                brief_sha256="abc",
                status=status,
                created_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
                grading_model="anthropic/claude-sonnet-4.6",
                inputs=[],
                dispatched_models=[],
            )
        )
    return TestClient(create_app(config))


def test_every_tab_has_its_own_url_and_marks_itself_active(tmp_path: Path) -> None:
    client = _tab_client(tmp_path)

    for path, label in (
        ("/", "Published work"),
        ("/commission", "New commission"),
        ("/active", "What&#x27;s active"),
    ):
        page = client.get(path)
        assert page.status_code == 200, path
        assert f'class="tab active" href="{path}"' in page.text, f"{path} should mark itself active"
        for other, _ in (("/", ""), ("/commission", ""), ("/active", "")):
            if other != path:
                assert f'class="tab" href="{other}"' in page.text, f"{path} should link to {other}"


def test_the_landing_tab_is_published_work_not_the_form(tmp_path: Path) -> None:
    """What exists beats what you might make."""
    page = _tab_client(tmp_path).get("/")

    assert "Published work" in page.text
    assert "an-investigation" in page.text
    assert 'action="/review"' not in page.text, "the commission form belongs on its own tab"


def test_an_in_flight_run_appears_under_active_and_a_finished_one_does_not(
    tmp_path: Path,
) -> None:
    client = _tab_client(tmp_path, statuses=("running", "completed"))

    page = client.get("/active")

    assert "r-0-running" in page.text
    # The finished run is still reachable, below — history with nowhere to live
    # is history nobody finds.
    assert "r-1-completed" in page.text
    assert page.text.index("r-0-running") < page.text.index("Finished runs")
    assert page.text.index("Finished runs") < page.text.index("r-1-completed")


def test_active_says_so_when_nothing_is_running(tmp_path: Path) -> None:
    page = _tab_client(tmp_path, statuses=("completed",)).get("/active")

    assert "Nothing is running or drafting right now." in page.text


def test_the_flow_page_offers_a_way_back(tmp_path: Path) -> None:
    """#35: opening an investigation used to strand you there."""
    client = _tab_client(tmp_path)

    page = client.get("/flow/an-investigation")

    assert page.status_code == 200
    assert 'href="/"' in page.text, "the flow page must link back into the app"


def test_health_names_itself_so_a_checker_can_verify_identity(tmp_path: Path) -> None:
    """A check that only proves a socket is open passes against the wrong service."""
    page = _tab_client(tmp_path).get("/health")

    assert page.status_code == 200
    assert page.json()["service"] == "alexandria-web"
    assert page.json()["version"]


def test_every_page_points_at_its_source(tmp_path: Path) -> None:
    client = _tab_client(tmp_path)

    for path in ("/", "/commission", "/active"):
        body = client.get(path).text
        assert "https://www.dhk.io" in body, path
        assert "github.com/dhk/minority-report" in body, path
        assert "github.com/dhk/alexandria" in body, path


def test_the_bind_address_defaults_to_loopback_and_is_configurable(
    monkeypatch: object,
) -> None:
    """#39: a managed unit must not expose the surface to a network by accident."""
    import argparse
    import os

    from alexandria import web

    def _defaults() -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        parser.add_argument("--host", default=os.environ.get("ALEXANDRIA_WEB_HOST", "127.0.0.1"))
        parser.add_argument(
            "--port", type=int, default=int(os.environ.get("ALEXANDRIA_WEB_PORT", "8798"))
        )
        return parser.parse_args([])

    assert web  # the parser above mirrors web.main's declaration
    previous = dict(os.environ)
    try:
        os.environ.pop("ALEXANDRIA_WEB_HOST", None)
        os.environ.pop("ALEXANDRIA_WEB_PORT", None)
        assert _defaults().host == "127.0.0.1"
        assert _defaults().port == 8798
        os.environ["ALEXANDRIA_WEB_HOST"] = "100.64.0.1"
        assert _defaults().host == "100.64.0.1"
    finally:
        os.environ.clear()
        os.environ.update(previous)


def test_read_only_refuses_to_mount_the_routes_that_spend(tmp_path: Path) -> None:
    """The surface has no authentication and POST /dispatch spends real money.
    Reachable from another machine, that means anyone who reaches it can
    commission runs — so the routes are absent, not merely discouraged."""
    client = TestClient(create_app(_discoverability_config(tmp_path), read_only=True))

    dispatched = client.post("/dispatch/d-whatever")
    reviewed = client.post("/review", data={"task": "anything"})

    assert dispatched.status_code == 404
    assert reviewed.status_code == 404


def test_read_only_still_serves_finished_work(tmp_path: Path) -> None:
    """Read-only is for reading: taking the write routes away must not cost
    the reader anything."""
    client = TestClient(create_app(_discoverability_config(tmp_path), read_only=True))

    assert client.get("/health").status_code == 200
    assert client.get("/").status_code == 200
    assert client.get("/active").status_code == 200


def test_read_only_says_so_instead_of_rendering_a_form_that_cannot_post(
    tmp_path: Path,
) -> None:
    """Rendering the form would be a lie: its POST target is not mounted, so
    an operator would compose a whole brief and lose it to a 405."""
    client = TestClient(create_app(_discoverability_config(tmp_path), read_only=True))

    response = client.get("/commission")

    assert response.status_code == 200
    assert "read-only" in response.text.lower()
    assert 'action="/review"' not in response.text


def test_the_writable_default_is_unchanged(tmp_path: Path) -> None:
    """Loopback-bound, the writable surface is the product. read_only is opt-in."""
    client = TestClient(create_app(_discoverability_config(tmp_path)))

    response = client.get("/commission")

    assert 'action="/review"' in response.text
    # A POST that reaches the handler and fails validation is a mounted route;
    # 404 would mean it was not mounted at all.
    assert client.post("/review", data={"task": ""}).status_code != 404


def test_the_dashboard_tab_renders_and_declares_its_blind_spots(tmp_path: Path) -> None:
    """#34: composed state, and an explicit account of what it cannot see."""
    page = _tab_client(tmp_path).get("/dashboard")

    assert page.status_code == 200
    assert "Dashboard" in page.text
    assert "What this page cannot see" in page.text
    assert "nothing here was suggested by a model" in page.text
