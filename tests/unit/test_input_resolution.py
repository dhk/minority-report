from pathlib import Path

import httpx
import pytest

from alexandria.input_resolution import (
    GitHubResolver,
    InputResolutionError,
    extract_input,
    validate_input_set,
)


@pytest.fixture
def no_github_token(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Isolate the resolver from any token the developer has configured locally."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("ALEXANDRIA_SECRETS_FILE", str(tmp_path / "missing.env"))


def test_extract_markdown_preserves_byte_checksum() -> None:
    item = extract_input("brief.md", b"# Brief\n\nResearch this.")
    assert item.state == "extracted"
    assert item.text.startswith("# Brief")
    assert item.bytes == 23
    assert len(item.sha256) == 64


def test_extract_html_returns_visible_text() -> None:
    item = extract_input("brief.html", b"<h1>Brief</h1><p>Research this.</p>")
    assert item.text == "Brief\nResearch this."
    assert item.extraction_method == "html.parser"


def test_unsupported_input_stays_visible_as_excluded() -> None:
    item = extract_input("image.png", b"not an image")
    assert item.state == "excluded"
    assert item.warning is not None


def test_validate_input_set_refuses_only_excluded_inputs() -> None:
    with pytest.raises(InputResolutionError):
        validate_input_set([extract_input("image.png", b"x")])


@pytest.mark.anyio
async def test_github_issue_becomes_plain_text_input() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/dhk/alexandria/issues/3"
        return httpx.Response(200, json={"title": "Research brief", "body": "Compare options."})

    client = httpx.AsyncClient(
        base_url="https://api.github.test", transport=httpx.MockTransport(handler)
    )
    async with client:
        resolver = GitHubResolver(client)
        inputs = await resolver.resolve("https://github.com/dhk/alexandria/issues/3")
    assert inputs[0].name == "issue-3.md"
    assert "Compare options." in inputs[0].text


def test_resolver_sends_bearer_token_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "gh-token")
    resolver = GitHubResolver()
    assert resolver.client.headers["Authorization"] == "Bearer gh-token"


@pytest.mark.usefixtures("no_github_token")
def test_resolver_stays_unauthenticated_without_a_token() -> None:
    resolver = GitHubResolver()
    assert "Authorization" not in resolver.client.headers
    assert resolver.client.headers["Accept"] == "application/vnd.github+json"


@pytest.mark.anyio
@pytest.mark.usefixtures("no_github_token")
async def test_unauthenticated_404_names_the_private_repo_case() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = httpx.AsyncClient(
        base_url="https://api.github.test", transport=httpx.MockTransport(handler)
    )
    async with client:
        resolver = GitHubResolver(client)
        with pytest.raises(InputResolutionError) as excinfo:
            await resolver.resolve("https://github.com/dhk/private/issues/1")
    assert "GITHUB_TOKEN" in str(excinfo.value)


@pytest.mark.anyio
async def test_authenticated_404_does_not_blame_the_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    client = httpx.AsyncClient(
        base_url="https://api.github.test",
        headers={"Authorization": "Bearer gh-token"},
        transport=httpx.MockTransport(handler),
    )
    async with client:
        resolver = GitHubResolver(client)
        with pytest.raises(InputResolutionError) as excinfo:
            await resolver.resolve("https://github.com/dhk/private/issues/1")
    assert "GITHUB_TOKEN" not in str(excinfo.value)
