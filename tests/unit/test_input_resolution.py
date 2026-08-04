import httpx
import pytest

from alexandria.input_resolution import (
    GitHubResolver,
    InputResolutionError,
    extract_input,
    validate_input_set,
)


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
