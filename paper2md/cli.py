"""
Command line interface (the Slack bot is started with app.py).

    uv run python -m paper2md translate <PDF file or URL> [--out DIR] [--gyazo] [--translation-model M] ...
    uv run python -m paper2md cache list
    uv run python -m paper2md cache show <key>
    uv run python -m paper2md cache remove <key> [<key> ...]
    uv run python -m paper2md cache prune --older-than <days>

``translate`` reads OPENAI_API_KEY (and GYAZO_ACCESS_TOKEN with --gyazo) from
the environment.  Cache keys look like ``arxiv:2408.03326v3`` or ``sha256:<hex>``.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .cache import PaperCache
from .config import Settings
from .models import EFFORT_LEVELS, ModelChoice
from .pdf import check_limits, fetch_pdf
from .sources import download_arxiv_pdf, find_paper_ref


def _fetch_source(source: str, pdf_path: Path, settings: Settings) -> None:
    if Path(source).is_file():
        if Path(source).resolve() != pdf_path.resolve():
            shutil.copyfile(source, pdf_path)
        return
    ref = find_paper_ref({}, source)
    if ref is None:
        raise SystemExit(f"Not a PDF file or a supported URL: {source}")
    max_bytes = settings.max_pdf_mb * 2**20
    if ref.kind == "arxiv":
        download_arxiv_pdf(ref.arxiv_id, ref.arxiv_version, pdf_path, max_bytes=max_bytes)
    else:
        fetch_pdf(ref.pdf_url, pdf_path, max_bytes=max_bytes)


def cmd_translate(args: argparse.Namespace) -> None:
    from .pipeline import translate_paper  # imports torch; keep `cache` commands fast

    defaults = Settings.from_env()
    choice = ModelChoice.resolve(
        {
            "translation_model": args.translation_model,
            "translation_effort": args.translation_effort,
            "summary_model": args.summary_model,
            "summary_effort": args.summary_effort,
        },
        # any model may be given on the command line, not only the ones offered in Slack
        replace(
            defaults,
            model_choices=(*defaults.model_choices, *filter(None, (args.translation_model, args.summary_model))),
        ),
    )
    settings = choice.apply(defaults)
    print(f"Models: {choice.describe()}", file=sys.stderr)
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    pdf_path = out / "paper.pdf"
    _fetch_source(args.source, pdf_path, settings)
    check_limits(pdf_path, settings.max_pages, settings.max_pdf_mb)
    result = translate_paper(
        pdf_path,
        out,
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        gyazo_token=os.getenv("GYAZO_ACCESS_TOKEN") if args.gyazo else None,
        settings=settings,
    )
    print(result.summary)
    print(f"\nMarkdown: {result.gyazo_markdown_path or result.markdown_path}")
    if result.failed_segments:
        print(f"Failed segments: {result.failed_segments}")


def cmd_cache_list(args: argparse.Namespace) -> None:
    for entry in PaperCache().entries():
        model = f"{entry.translation_model}/{entry.translation_effort or '-'}"
        print(f"{entry.key}\t{entry.created_date}\t{model}\t{entry.title or entry.display_name}")


def cmd_cache_show(args: argparse.Namespace) -> None:
    entry = PaperCache().get(args.key)
    if entry is None:
        raise SystemExit(f"Not in the cache: {args.key}")
    print(entry.path)
    print(json.dumps(entry.manifest, ensure_ascii=False, indent=2))


def cmd_cache_remove(args: argparse.Namespace) -> None:
    cache = PaperCache()
    for key in args.keys:
        print(f"{key}: {'removed' if cache.remove(key) else 'not found'}")


def cmd_cache_prune(args: argparse.Namespace) -> None:
    cache = PaperCache()
    limit = datetime.now(timezone.utc) - timedelta(days=args.older_than)
    removed = 0
    for entry in list(cache.entries()):
        try:
            created = datetime.fromisoformat(entry.created_at)
        except ValueError:
            continue
        if created < limit and cache.remove(entry.key):
            removed += 1
            print(f"removed {entry.key}")
    removed += cache.purge_stale()
    print(f"{removed} directories removed")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m paper2md", description=__doc__.split("\n\n")[0].strip())
    commands = parser.add_subparsers(dest="command", required=True)

    translate = commands.add_parser("translate", help="translate a PDF file or a paper URL")
    translate.add_argument("source", help="PDF file, arXiv / OpenReview / ACL Anthology link or PDF URL")
    translate.add_argument("--out", default="results", help="output directory (default: results)")
    translate.add_argument("--gyazo", action="store_true", help="also write a Markdown with Gyazo image URLs")
    translate.add_argument("--translation-model", help="default: $TRANSLATION_MODEL or gpt-6.1-terra")
    translate.add_argument(
        "--translation-effort", choices=EFFORT_LEVELS, help="default: $TRANSLATION_REASONING_EFFORT or none"
    )
    translate.add_argument("--summary-model", help="default: $SUMMARY_MODEL or gpt-6.1-sol")
    translate.add_argument(
        "--summary-effort", choices=EFFORT_LEVELS, help="default: $SUMMARY_REASONING_EFFORT or medium"
    )
    translate.set_defaults(func=cmd_translate)

    cache = commands.add_parser("cache", help="inspect or clean the translation cache").add_subparsers(
        dest="cache_command", required=True
    )
    cache.add_parser("list", help="list cached papers").set_defaults(func=cmd_cache_list)
    show = cache.add_parser("show", help="print the manifest of an entry")
    show.add_argument("key")
    show.set_defaults(func=cmd_cache_show)
    remove = cache.add_parser("remove", help="delete entries")
    remove.add_argument("keys", nargs="+")
    remove.set_defaults(func=cmd_cache_remove)
    prune = cache.add_parser("prune", help="delete entries older than N days and leftovers of crashed jobs")
    prune.add_argument("--older-than", type=float, required=True, metavar="DAYS")
    prune.set_defaults(func=cmd_cache_prune)

    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), stream=sys.stderr)
    args.func(args)


if __name__ == "__main__":
    main()
