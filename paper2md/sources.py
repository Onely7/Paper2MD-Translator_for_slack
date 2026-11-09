"""
Identify the paper a message points to and derive stable cache keys.

Supported sources (checked in this order):

* a PDF attached in Slack,
* arXiv links (``/abs/``, ``/pdf/``, ``/html/``, with or without a version),
  also through Hugging Face Papers and alphaXiv,
* OpenReview (``/forum?id=`` or ``/pdf?id=``) and ACL Anthology links,
* any other URL ending in ``.pdf``.

arXiv papers are keyed by ``arxiv:<id>v<version>``.  A link without a version
means the latest version, which is resolved when the request arrives, so a
cached v2 is not returned once v3 has been published.  Everything else is
keyed by the SHA-256 of the downloaded PDF.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from .pdf import HTTP_HEADERS, fetch_pdf, safe_pdf_filename

logger = logging.getLogger(__name__)

ARXIV_API_URL = "https://export.arxiv.org/api/query"
# New-style ids (2408.03326) and old-style ids (hep-th/9901001, math.GT/0309136)
_ARXIV_ID = r"(?P<id>\d{4}\.\d{4,5}|[a-z][a-z\-]*(?:\.[a-z]{2})?/\d{7})(?:v(?P<version>\d+))?"
_END = r"(?![\w.])"
ARXIV_URL_RES = (
    re.compile(rf"https?://(?:www\.|export\.)?arxiv\.org/(?:abs|pdf|html)/{_ARXIV_ID}(?:\.pdf)?{_END}", re.I),
    re.compile(
        rf"https?://(?:www\.)?(?:huggingface\.co/papers|alphaxiv\.org/(?:abs|overview))/{_ARXIV_ID}{_END}", re.I
    ),
)
OPENREVIEW_URL_RE = re.compile(r"https?://openreview\.net/(?:forum|pdf)\?id=(?P<id>[\w-]+)", re.IGNORECASE)
ACL_URL_RE = re.compile(r"https?://aclanthology\.org/(?P<id>[A-Za-z0-9][\w.-]*?)(?:\.pdf)?/?(?=[\s<>|`'\")\]]|$)")
# A URL ending in .pdf (optionally with a query); stops at Slack's "<url|label>" markup.
PDF_URL_RE = re.compile(r'https?://[^\s<>"|`]+?\.pdf(?:\?[^\s<>"|`]*)?(?=[\s<>"|`\')\]]|$)', re.IGNORECASE)


@dataclass(frozen=True)
class PaperRef:
    """Where the requested paper comes from."""

    kind: str  # "arxiv" | "url" | "slack_file"
    url: str  # as written in the message
    pdf_url: str  # where the PDF is downloaded from (arXiv: see arxiv_pdf_url)
    arxiv_id: str | None = None
    arxiv_version: int | None = None  # version written in the link, if any
    filename: str | None = None  # name of a Slack upload
    name: str | None = None  # display name for other sources

    def display_name(self, arxiv_version: int | None = None) -> str:
        """File name stem used for the files posted to Slack."""
        if self.kind == "arxiv":
            version = arxiv_version or self.arxiv_version
            return self.arxiv_id.replace("/", "_") + (f"v{version}" if version else "")
        if self.kind == "slack_file":
            return Path(safe_pdf_filename(self.filename)).stem
        return self.name or Path(safe_pdf_filename(urlparse(self.pdf_url).path)).stem


def find_paper_ref(event: dict, text: str | None) -> PaperRef | None:
    """
    Return the paper referenced by a Slack message (attachments win over links).

    >>> find_paper_ref({}, "<@U1> <https://arxiv.org/abs/2408.03326>").arxiv_id
    '2408.03326'
    >>> find_paper_ref({}, "<https://arxiv.org/pdf/2408.03326v2.pdf>").arxiv_version
    2
    >>> find_paper_ref({}, "<https://openreview.net/forum?id=AbC-12>").pdf_url
    'https://openreview.net/pdf?id=AbC-12'
    >>> find_paper_ref({}, "`https://example.com/a/paper.pdf`").url
    'https://example.com/a/paper.pdf'
    """
    for f in event.get("files") or []:
        if f.get("mimetype") == "application/pdf" or f.get("filetype") == "pdf":
            if url := f.get("url_private_download"):
                return PaperRef("slack_file", url, url, filename=f.get("name"))

    text = text or ""
    for pattern in ARXIV_URL_RES:
        if match := pattern.search(text):
            version = int(match.group("version")) if match.group("version") else None
            return PaperRef(
                "arxiv",
                match.group(0),
                arxiv_pdf_url(match.group("id"), version),
                arxiv_id=match.group("id"),
                arxiv_version=version,
            )
    if match := OPENREVIEW_URL_RE.search(text):
        paper_id = match.group("id")
        return PaperRef(
            "url", match.group(0), f"https://openreview.net/pdf?id={paper_id}", name=f"openreview-{paper_id}"
        )
    if match := ACL_URL_RE.search(text):
        paper_id = match.group("id")
        return PaperRef("url", match.group(0), f"https://aclanthology.org/{paper_id}.pdf", name=paper_id)
    if match := PDF_URL_RE.search(text):
        return PaperRef("url", match.group(0), match.group(0))
    return None


# ---------------------------------------------------------------------------
# Cache keys
# ---------------------------------------------------------------------------
def arxiv_key(arxiv_id: str, version: int) -> str:
    return f"arxiv:{arxiv_id}v{version}"


def sha256_key(digest: str) -> str:
    return f"sha256:{digest}"


# ---------------------------------------------------------------------------
# arXiv versions
# ---------------------------------------------------------------------------
def arxiv_pdf_url(arxiv_id: str, version: int | None = None) -> str:
    return f"https://arxiv.org/pdf/{arxiv_id}" + (f"v{version}" if version else "")


def version_from_headers(headers) -> int | None:
    """
    arXiv names the PDF it serves, e.g. ``inline; filename="2408.03326v3.pdf"``.

    >>> version_from_headers({"content-disposition": 'inline; filename="2408.03326v3.pdf"'})
    3
    """
    disposition = headers.get("content-disposition") or headers.get("Content-Disposition") or ""
    match = re.search(r"v(\d+)\.pdf", disposition, flags=re.IGNORECASE)
    return int(match.group(1)) if match else None


def version_from_atom(atom: str, arxiv_id: str) -> int | None:
    """
    >>> version_from_atom("<entry><id>http://arxiv.org/abs/2408.03326v3</id></entry>", "2408.03326")
    3
    """
    match = re.search(rf"<id>\s*https?://arxiv\.org/abs/{re.escape(arxiv_id)}v(\d+)\s*</id>", atom)
    return int(match.group(1)) if match else None


def resolve_latest_arxiv_version(arxiv_id: str, timeout: float = 20.0) -> int | None:
    """Latest version of an arXiv paper, or None if it cannot be found out.

    A HEAD request on the versionless PDF URL is enough (arXiv names the file
    it would serve); the arXiv API is the fallback.
    """
    try:
        response = requests.head(arxiv_pdf_url(arxiv_id), allow_redirects=True, timeout=timeout, headers=HTTP_HEADERS)
        if response.ok and (version := version_from_headers(response.headers)):
            return version
    except requests.RequestException as e:
        logger.warning("arXiv HEAD request failed: %s", e)
    try:
        response = requests.get(
            ARXIV_API_URL, params={"id_list": arxiv_id, "max_results": 1}, timeout=timeout, headers=HTTP_HEADERS
        )
        response.raise_for_status()
        return version_from_atom(response.text, arxiv_id)
    except requests.RequestException as e:
        logger.warning("arXiv API request failed: %s", e)
    return None


def download_arxiv_pdf(
    arxiv_id: str, version: int | None, path: str | Path, max_bytes: int | None = None
) -> int | None:
    """Download an arXiv PDF and return the version that was actually served."""
    headers = fetch_pdf(arxiv_pdf_url(arxiv_id, version), path, max_bytes=max_bytes)
    return version_from_headers(headers) or version
