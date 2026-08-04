"""Extract local inputs and resolve supported GitHub repository URLs."""

from __future__ import annotations

import base64
import hashlib
import io
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Self
from urllib.parse import urlparse

import httpx
from pypdf import PdfReader

from alexandria.commission_models import InputArtifact, InputState

MAX_FILES = 8
MAX_BYTES = 20 * 1024 * 1024
MAX_CHARS = 400_000
SUPPORTED_SUFFIXES = {".pdf", ".html", ".htm", ".txt", ".md", ".markdown"}
TEXT_SUFFIXES = {".txt", ".md", ".markdown"}


class InputResolutionError(ValueError):
    """Input content cannot be accepted without silently losing evidence."""


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data.strip():
            self.parts.append(data.strip())

    def text(self) -> str:
        return "\n".join(self.parts)


def _decode_text(raw: bytes) -> tuple[str, str, str | None]:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding), encoding, None
        except UnicodeDecodeError:
            pass
    return (
        raw.decode("latin-1"),
        "latin-1",
        "Decoded as latin-1 after UTF-8 failed; verify characters before dispatch.",
    )


def extract_input(name: str, raw: bytes, *, source_url: str | None = None) -> InputArtifact:
    """Extract one supported input while preserving original-byte metadata."""
    suffix = Path(name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        return InputArtifact(
            name=name,
            format=suffix.removeprefix(".") or "unknown",
            bytes=len(raw),
            extracted_chars=0,
            encoding=None,
            sha256=hashlib.sha256(raw).hexdigest(),
            state="excluded",
            warning="Unsupported format; only PDF, HTML, text, and Markdown are dispatched.",
            source_url=source_url,
            extraction_method="none",
            text="",
            original_base64=base64.b64encode(raw).decode(),
        )

    warning: str | None = None
    encoding: str | None = None
    if suffix == ".pdf":
        try:
            reader = PdfReader(io.BytesIO(raw))
            pages = [(page.extract_text() or "") for page in reader.pages]
        except Exception as exc:
            raise InputResolutionError(f"{name}: PDF extraction failed: {exc}") from exc
        text = "\n\n".join(pages)
        empty_pages = sum(not page.strip() for page in pages)
        if empty_pages:
            warning = (
                f"{empty_pages} PDF page(s) produced no text and reach the models effectively empty; "
                "OCR is not performed."
            )
        method = "pypdf"
    else:
        decoded, encoding, warning = _decode_text(raw)
        if suffix in {".html", ".htm"}:
            parser = _TextExtractor()
            parser.feed(decoded)
            text = parser.text()
            method = "html.parser"
        else:
            text = decoded
            method = "text-decode"

    state: InputState = "warning" if warning else "extracted"
    return InputArtifact(
        name=name,
        format=suffix.removeprefix("."),
        bytes=len(raw),
        extracted_chars=len(text),
        encoding=encoding,
        sha256=hashlib.sha256(raw).hexdigest(),
        state=state,
        warning=warning,
        source_url=source_url,
        extraction_method=method,
        text=text,
        original_base64=base64.b64encode(raw).decode(),
    )


def validate_input_set(inputs: list[InputArtifact]) -> None:
    if not inputs:
        raise InputResolutionError("Provide pasted content, a supported file, or a GitHub URL.")
    if len(inputs) > MAX_FILES:
        raise InputResolutionError(
            f"At most {MAX_FILES} inputs may be dispatched; got {len(inputs)}."
        )
    if sum(item.bytes for item in inputs) > MAX_BYTES:
        raise InputResolutionError("Input bytes exceed the 20 MB commission limit.")
    if sum(item.extracted_chars for item in inputs) > MAX_CHARS:
        raise InputResolutionError("Extracted text exceeds the 400,000 character limit.")
    if not any(item.state != "excluded" and item.text.strip() for item in inputs):
        raise InputResolutionError("No dispatchable text was extracted from the supplied inputs.")


def pasted_input(text: str) -> InputArtifact | None:
    if not text:
        return None
    return extract_input("pasted-content.md", text.encode())


def _github_parts(url: str) -> tuple[str, str, list[str]]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise InputResolutionError("Repository resolution currently accepts HTTPS github.com URLs.")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        raise InputResolutionError("GitHub URL must include an owner and repository.")
    return parts[0], parts[1].removesuffix(".git"), parts[2:]


def _priority(path: str) -> tuple[int, int, str]:
    lowered = path.lower()
    keyword = bool(re.search(r"(^|[-_/])(brief|design|research|rfc|readme)([-_. /]|$)", lowered))
    return (0 if keyword else 1, lowered.count("/"), lowered)


class GitHubResolver:
    """Resolve repository, issue, PR, and blob URLs through GitHub's public API."""

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(
            base_url="https://api.github.com",
            headers={"Accept": "application/vnd.github+json", "User-Agent": "alexandria/0.1"},
            timeout=30,
        )
        self._owns_client = client is None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._owns_client:
            await self.client.aclose()

    async def _json(self, path: str) -> object:
        response = await self.client.get(path)
        if response.status_code >= 400:
            raise InputResolutionError(
                f"GitHub resolution failed ({response.status_code}) for {path}."
            )
        return response.json()

    async def _content(self, owner: str, repo: str, path: str, ref: str) -> InputArtifact:
        payload = await self._json(f"/repos/{owner}/{repo}/contents/{path}?ref={ref}")
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise InputResolutionError(f"GitHub path is not a file: {path}")
        encoded = payload.get("content")
        if not isinstance(encoded, str):
            raise InputResolutionError(f"GitHub did not return file content for {path}.")
        raw = base64.b64decode(encoded)
        return extract_input(path, raw, source_url=str(payload.get("html_url") or ""))

    async def resolve(self, url: str) -> list[InputArtifact]:
        owner, repo, tail = _github_parts(url)
        if tail[:1] == ["issues"] and len(tail) == 2 and tail[1].isdigit():
            payload = await self._json(f"/repos/{owner}/{repo}/issues/{tail[1]}")
            if not isinstance(payload, dict) or "pull_request" in payload:
                raise InputResolutionError("Use the pull-request URL for a GitHub PR.")
            body = str(payload.get("body") or "")
            title = str(payload.get("title") or "Untitled issue")
            return [
                extract_input(
                    f"issue-{tail[1]}.md", f"# {title}\n\n{body}".encode(), source_url=url
                )
            ]

        if tail[:1] == ["pull"] and len(tail) == 2 and tail[1].isdigit():
            pr = await self._json(f"/repos/{owner}/{repo}/pulls/{tail[1]}")
            files = await self._json(f"/repos/{owner}/{repo}/pulls/{tail[1]}/files?per_page=100")
            if not isinstance(pr, dict) or not isinstance(files, list):
                raise InputResolutionError("GitHub returned an unexpected pull-request shape.")
            head = pr.get("head")
            ref = str(head.get("sha")) if isinstance(head, dict) else ""
            paths = [
                str(item.get("filename"))
                for item in files
                if isinstance(item, dict)
                and Path(str(item.get("filename"))).suffix.lower() in SUPPORTED_SUFFIXES
                and item.get("status") != "removed"
            ]
            paths.sort(key=_priority)
            selected = paths[:MAX_FILES]
            if not selected:
                body = str(pr.get("body") or "")
                title = str(pr.get("title") or "Untitled pull request")
                return [
                    extract_input(
                        f"pr-{tail[1]}.md", f"# {title}\n\n{body}".encode(), source_url=url
                    )
                ]
            return [await self._content(owner, repo, path, ref) for path in selected]

        if tail[:1] == ["blob"] and len(tail) >= 3:
            ref, path = tail[1], "/".join(tail[2:])
            return [await self._content(owner, repo, path, ref)]

        if tail:
            raise InputResolutionError("Unsupported GitHub URL shape.")

        repository = await self._json(f"/repos/{owner}/{repo}")
        if not isinstance(repository, dict):
            raise InputResolutionError("GitHub returned an unexpected repository shape.")
        ref = str(repository.get("default_branch") or "main")
        tree = await self._json(f"/repos/{owner}/{repo}/git/trees/{ref}?recursive=1")
        entries = tree.get("tree") if isinstance(tree, dict) else None
        if not isinstance(entries, list):
            raise InputResolutionError("GitHub did not return a repository tree.")
        paths = [
            str(entry.get("path"))
            for entry in entries
            if isinstance(entry, dict)
            and entry.get("type") == "blob"
            and Path(str(entry.get("path"))).suffix.lower() in SUPPORTED_SUFFIXES
            and int(entry.get("size") or 0) <= MAX_BYTES
        ]
        paths.sort(key=_priority)
        if not paths:
            raise InputResolutionError("The repository has no supported brief-like files.")
        return [await self._content(owner, repo, path, ref) for path in paths[:MAX_FILES]]
