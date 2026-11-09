"""
All settings that can be changed with environment variables, in one place.

Relative paths are resolved against the repository root.  Call
``Settings.from_env()`` where a value is needed instead of caching it at import
time, so that a changed environment (and tests) take effect immediately.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, fields, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DEFAULT_TRANSLATION_MODEL = "gpt-6.1-terra"
DEFAULT_SUMMARY_MODEL = "gpt-6.1-sol"


def _str(name: str, default: str | None) -> str | None:
    value = os.getenv(name)
    return value.strip() if value and value.strip() else default


def _int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(_str(name, str(default))))
    except ValueError:
        return default


def _bool(name: str, default: bool = False) -> bool:
    value = _str(name, None)
    return default if value is None else value.lower() in {"1", "true", "yes", "on"}


def _path(name: str, default: str) -> Path:
    path = Path(_str(name, default)).expanduser()
    return path if path.is_absolute() else REPO_ROOT / path


def _default_no_cache_dir() -> Path | None:
    shm = Path("/dev/shm")
    return shm if shm.is_dir() and os.access(shm, os.W_OK | os.X_OK) else None


@dataclass(frozen=True)
class Settings:
    # OpenAI
    translation_model: str = DEFAULT_TRANSLATION_MODEL
    summary_model: str = DEFAULT_SUMMARY_MODEL
    translation_effort: str = "none"
    summary_effort: str = "medium"
    image_detail: str = "auto"
    openai_max_concurrency: int = 8
    # PDF
    page_resolution: int = 4000  # longest side (px) of the rendered pages
    max_pages: int = 80
    max_pdf_mb: int = 100
    # Dolphin-v2
    dolphin_model_path: Path = REPO_ROOT / "models/Dolphin-v2"
    dolphin_device: str | None = None  # default: "cuda" if available
    dolphin_dtype: str | None = None  # default: bf16 on Ampere+, fp16 on older GPUs
    dolphin_attn_impl: str = "sdpa"
    dolphin_max_batch_size: int = 4
    dolphin_keep_loaded: bool = False
    dolphin_recognize_labels: str | None = None  # "all" or "label,label,..."
    # storage
    cache_dir: Path = REPO_ROOT / "data/cache"
    no_cache_dir: Path | None = None  # default: /dev/shm, else the system temp dir
    keystore_path: Path = REPO_ROOT / "user_keys.db"
    # Slack bot
    max_concurrent_jobs: int = 2

    @classmethod
    def from_env(cls, **overrides) -> Settings:
        no_cache = _str("NO_CACHE_WORKDIR", None)
        settings = cls(
            translation_model=_str("TRANSLATION_MODEL", DEFAULT_TRANSLATION_MODEL),
            summary_model=_str("SUMMARY_MODEL", DEFAULT_SUMMARY_MODEL),
            translation_effort=_str("TRANSLATION_REASONING_EFFORT", "none"),
            summary_effort=_str("SUMMARY_REASONING_EFFORT", "medium"),
            image_detail=_str("TRANSLATION_IMAGE_DETAIL", "auto"),
            openai_max_concurrency=_int("OPENAI_MAX_CONCURRENCY", 8),
            page_resolution=_int("PAGE_RESOLUTION", 4000, minimum=1000),
            max_pages=_int("MAX_PDF_PAGES", 80),
            max_pdf_mb=_int("MAX_PDF_MB", 100),
            dolphin_model_path=_path("DOLPHIN_MODEL_PATH", "models/Dolphin-v2"),
            dolphin_device=_str("DOLPHIN_DEVICE", None),
            dolphin_dtype=_str("DOLPHIN_DTYPE", None),
            dolphin_attn_impl=_str("DOLPHIN_ATTN_IMPL", "sdpa"),
            dolphin_max_batch_size=_int("DOLPHIN_MAX_BATCH_SIZE", 4),
            dolphin_keep_loaded=_bool("DOLPHIN_KEEP_LOADED"),
            dolphin_recognize_labels=_str("DOLPHIN_RECOGNIZE_LABELS", None),
            cache_dir=_path("PAPER_CACHE_DIR", "data/cache"),
            no_cache_dir=Path(no_cache).expanduser() if no_cache else _default_no_cache_dir(),
            keystore_path=_path("KEY_DB_PATH", "user_keys.db"),
            max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 2),
        )
        known = {f.name for f in fields(cls)}
        unknown = set(overrides) - known
        if unknown:
            raise TypeError(f"Unknown settings: {sorted(unknown)}")
        return replace(settings, **{k: v for k, v in overrides.items() if v is not None})
