"""Figure/table images: export next to the Markdown and optional Gyazo upload."""

from __future__ import annotations

import html
import logging
import shutil
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

logger = logging.getLogger(__name__)

GYAZO_UPLOAD_URL = "https://upload.gyazo.com/api/upload"


def img_tag(src: str, alt: str) -> str:
    return f'<img src="{html.escape(src, quote=True)}" width=60% alt="{html.escape(alt, quote=True)}">'


def export_figures(crops: list[str], figures_dir: str | Path) -> list[Path]:
    """Copy the figure crops to ``figures_dir`` as fig01.png, fig02.png, ..."""
    figures_dir = Path(figures_dir)
    figures_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for number, crop in enumerate(crops, start=1):
        dest = figures_dir / f"fig{number:02d}.png"
        shutil.copyfile(crop, dest)
        paths.append(dest)
    return paths


def upload_to_gyazo(path: str | Path, access_token: str, retries: int = 3, timeout: float = 60.0) -> str:
    """Upload an image to Gyazo and return its URL.

    The token is sent in the POST body, never on a command line where other
    users of the server could read it with ``ps``.
    """
    last_error: Exception | None = None
    for attempt in range(retries):
        try:
            with open(path, "rb") as f:
                response = requests.post(
                    GYAZO_UPLOAD_URL,
                    data={"access_token": access_token},
                    files={"imagedata": (Path(path).name, f, "image/png")},
                    timeout=timeout,
                )
            if response.status_code in (401, 403):
                raise PermissionError(f"Gyazo rejected the access token (HTTP {response.status_code})")
            response.raise_for_status()
            url = response.json().get("url")
            if not url:
                raise RuntimeError("Gyazo did not return an image URL")
            return url
        except PermissionError:
            raise
        except Exception as e:  # network errors, 5xx, invalid JSON
            last_error = e
            if attempt < retries - 1:
                time.sleep(2**attempt)
    raise RuntimeError(f"Gyazo upload failed: {last_error}") from last_error


def upload_all_to_gyazo(paths: list[Path], access_token: str, max_workers: int = 4) -> list[str | None]:
    """Upload images in parallel; a failed upload yields None."""

    def upload(path: Path) -> str | None:
        try:
            return upload_to_gyazo(path, access_token)
        except Exception as e:
            logger.error("Gyazo upload failed for %s: %s", path.name, e)
            return None

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        return list(pool.map(upload, paths))
