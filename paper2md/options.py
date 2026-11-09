"""
Options a user can write in the Slack message that requests a translation.

    @Paper2MD-Translator --no-cache https://arxiv.org/abs/2304.08485

``--no-cache`` (also ``-no-cache``, ``no-cache``, ``nocache``, ``no_cache``,
``キャッシュなし``, ``保存しない`` ...) asks the server not to keep anything:
the PDF, intermediate files and outputs only live in a private temporary
directory that is deleted as soon as the results are posted.  A paper that is
already in the cache is still answered from it (nothing new is stored).

``--refresh`` (also ``--force``, ``再翻訳``) ignores the cached result and
translates the paper again; in normal mode the new result replaces the cached one.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class RequestOptions:
    no_cache: bool = False
    refresh: bool = False


# Hyphen-like characters that editors may substitute for "-" (e.g. "--" -> "—").
_DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−﹘﹣"), "-")
# Slack markup: <@U123>, <#C123|general>, <https://example.com|label>, <!here>
_SLACK_MARKUP = re.compile(r"<[^<>\s][^<>]*>")
_NO_CACHE_EN = re.compile(r"(?<![\w/.?&=-])-{0,2}no[-_ ]?cache(?![\w-])", re.IGNORECASE)
_NO_CACHE_JA = re.compile(r"キャッシュ(?:なし|無し|しない|オフ)|保存(?:なし|無し|しない)|データを?残さない")
_REFRESH_EN = re.compile(r"(?<![\w/.?&=-])-{1,2}(?:refresh|force)(?![\w-])", re.IGNORECASE)
_REFRESH_JA = re.compile(r"再翻訳")


def parse_request_options(text: str | None) -> RequestOptions:
    """
    >>> parse_request_options("<@U1> --no-cache <https://arxiv.org/abs/2304.08485>").no_cache
    True
    >>> parse_request_options("<@U1> —no-cache https://arxiv.org/abs/2304.08485").no_cache
    True
    >>> parse_request_options("<@U1> キャッシュなしで https://arxiv.org/abs/2304.08485").no_cache
    True
    >>> parse_request_options("<@U1> <https://example.com/paper.pdf?nocache=1>").no_cache
    False
    >>> parse_request_options("<@U1> https://arxiv.org/abs/2304.08485").no_cache
    False
    >>> parse_request_options("<@U1> --refresh <https://arxiv.org/abs/2304.08485>").refresh
    True
    """
    if not text:
        return RequestOptions()
    normalized = unicodedata.normalize("NFKC", text).translate(_DASHES)
    normalized = _SLACK_MARKUP.sub(" ", normalized)
    return RequestOptions(
        no_cache=bool(_NO_CACHE_EN.search(normalized) or _NO_CACHE_JA.search(normalized)),
        refresh=bool(_REFRESH_EN.search(normalized) or _REFRESH_JA.search(normalized)),
    )
