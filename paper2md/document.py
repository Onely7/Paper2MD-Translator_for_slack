"""
Layout analysis with Dolphin-v2 (https://github.com/bytedance/Dolphin).

The upstream code is vendored in ``Dolphin/`` with import patches only.  This
module adds what the application needs on top of it:

* GPU-aware precision: upstream always uses bf16 on CUDA, which is emulated
  and very slow on pre-Ampere GPUs such as the RTX 20xx, so fp16 is used there,
* a memory-efficient replacement for the Qwen2.5-VL vision attention
  (``_patch_qwen2_5_vl_vision_attention``),
* one model shared by concurrent jobs, with GPU inference serialized and the
  GPU memory released when no job needs the model,
* reading the text only of the labels the pipeline uses, and only on pages
  without a text layer (stage 2 dominates Dolphin's run time).
"""

from __future__ import annotations

import gc
import json
import logging
import threading
import time
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from pathlib import Path

import torch
import torch.nn.functional as F
from PIL import Image
from transformers import AutoProcessor, Qwen2_5_VLForConditionalGeneration

from Dolphin.demo_page import DOLPHIN, process_element_batch, process_elements
from Dolphin.utils.utils import check_bbox_overlap, parse_layout_string, process_coordinates

from .config import Settings

logger = logging.getLogger(__name__)

LAYOUT_PROMPT = "Parse the reading order of this document."
TEXT_PROMPT = "Read text in the image."  # stage-2 prompts of upstream process_elements
LABEL_PROMPTS = {
    "tab": "Parse the table in the image.",
    "equ": "Read formula in the image.",
    "code": "Read code in the image.",
}


def _select_device(configured: str | None) -> str:
    if configured:
        return configured
    return "cuda" if torch.cuda.is_available() else "cpu"


def _select_dtype(configured: str | None, device: str) -> torch.dtype:
    if configured:
        dtype = getattr(torch, configured, None)
        if not isinstance(dtype, torch.dtype):
            raise ValueError(f"Unknown DOLPHIN_DTYPE: {configured!r}")
        return dtype
    if device.startswith("cuda"):
        index = torch.device(device).index
        major, _minor = torch.cuda.get_device_capability(0 if index is None else index)
        # bf16 is only native on Ampere (sm_80) or newer; Turing/Volta emulate it
        # at a fraction of the fp16 speed and lack bf16 attention kernels.
        return torch.bfloat16 if major >= 8 else torch.float16
    return torch.float32


# ---------------------------------------------------------------------------
# Memory-efficient vision attention
# ---------------------------------------------------------------------------
def _varlen_sdpa(q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, cu_seqlens: torch.Tensor) -> torch.Tensor:
    """Attention restricted to the segments delimited by ``cu_seqlens``.

    ``q``/``k``/``v`` have shape ``[seq, heads, head_dim]``.  Equivalent to the
    block-diagonal boolean mask used by transformers' SDPA implementation, but
    segments of equal length are batched and no ``seq x seq`` mask is built, so
    PyTorch can use the memory-efficient kernel instead of materializing a
    ``heads x seq x seq`` attention matrix (3 GiB for one page image).
    """
    out = torch.empty_like(q)
    bounds = cu_seqlens.tolist()
    starts_by_length: dict[int, list[int]] = {}
    for start, end in zip(bounds[:-1], bounds[1:], strict=True):
        if end > start:
            starts_by_length.setdefault(end - start, []).append(start)

    for length, starts in starts_by_length.items():
        index = torch.tensor(starts, device=q.device).unsqueeze(1) + torch.arange(length, device=q.device)
        qs, ks, vs = (t[index].transpose(1, 2) for t in (q, k, v))  # [n, heads, length, dim]
        attn = F.scaled_dot_product_attention(qs, ks, vs, dropout_p=0.0)
        out[index] = attn.transpose(1, 2).to(out.dtype)
    return out


def _patch_qwen2_5_vl_vision_attention() -> None:
    """Swap transformers' masked SDPA vision attention for ``_varlen_sdpa``.

    transformers 4.51 builds a dense ``[1, seq, seq]`` mask for the vision
    tower.  With torch 2.1 the memory-efficient kernel then expands it per head,
    which runs out of memory on 11 GB GPUs for a single 1600px page.
    """
    from transformers.models.qwen2_5_vl import modeling_qwen2_5_vl as qwen_vl

    cls = qwen_vl.Qwen2_5_VLVisionSdpaAttention
    if getattr(cls, "_paper2md_varlen", False):
        return
    apply_rotary = qwen_vl.apply_rotary_pos_emb_vision

    def forward(self, hidden_states, cu_seqlens, rotary_pos_emb=None, position_embeddings=None):
        seq_length = hidden_states.shape[0]
        q, k, v = self.qkv(hidden_states).reshape(seq_length, 3, self.num_heads, -1).permute(1, 0, 2, 3).unbind(0)
        if position_embeddings is None:
            emb = torch.cat((rotary_pos_emb, rotary_pos_emb), dim=-1)
            cos, sin = emb.cos(), emb.sin()
        else:
            cos, sin = position_embeddings
        q, k = apply_rotary(q, k, cos, sin)
        return self.proj(_varlen_sdpa(q, k, v, cu_seqlens).reshape(seq_length, -1))

    cls.forward = forward
    cls._paper2md_varlen = True


# ---------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------
class DolphinV2(DOLPHIN):
    """Upstream ``DOLPHIN`` with explicit device/precision and thread-safe inference."""

    def __init__(self, model_path: Path, device: str | None, dtype: str | None, attn_impl: str) -> None:
        # Not calling super().__init__(): upstream loads in fp32 and then casts
        # to bf16 on any CUDA device.
        if not Path(model_path).exists():
            raise FileNotFoundError(
                f"Dolphin-v2 model not found at {model_path}. Download it with "
                "`uv run hf download ByteDance/Dolphin-v2 --local-dir models/Dolphin-v2` "
                "or set DOLPHIN_MODEL_PATH."
            )
        self.device = _select_device(device)
        self.dtype = _select_dtype(dtype, self.device)
        if attn_impl == "sdpa":
            _patch_qwen2_5_vl_vision_attention()

        started = time.monotonic()
        self.processor = AutoProcessor.from_pretrained(model_path, use_fast=False)
        self.model = Qwen2_5_VLForConditionalGeneration.from_pretrained(
            model_path, torch_dtype=self.dtype, attn_implementation=attn_impl, low_cpu_mem_usage=True
        )
        self.model.to(self.device)
        self.model.eval()
        self.tokenizer = self.processor.tokenizer
        self.tokenizer.padding_side = "left"
        self._lock = threading.Lock()
        logger.info(
            "Dolphin-v2 loaded on %s (%s, attn=%s) in %.1fs",
            self.device,
            str(self.dtype).removeprefix("torch."),
            attn_impl,
            time.monotonic() - started,
        )

    def chat(self, prompt, image):
        # Serialize GPU work: concurrent jobs share this instance.
        with self._lock, torch.inference_mode():
            return super().chat(prompt, image)

    def release(self) -> None:
        """Drop the weights even if callers still hold this wrapper."""
        self.model = self.processor = self.tokenizer = None


class _ModelManager:
    """Shares one Dolphin-v2 instance between concurrent jobs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._model: DolphinV2 | None = None
        self._key: tuple | None = None
        self._users = 0

    @contextmanager
    def use(self, settings: Settings) -> Iterator[DolphinV2]:
        key = (
            str(settings.dolphin_model_path),
            settings.dolphin_device,
            settings.dolphin_dtype,
            settings.dolphin_attn_impl,
        )
        with self._lock:
            if self._model is not None and self._key != key and self._users == 0:
                self._unload_locked()
            if self._model is None:
                self._model = DolphinV2(
                    settings.dolphin_model_path,
                    settings.dolphin_device,
                    settings.dolphin_dtype,
                    settings.dolphin_attn_impl,
                )
                self._key = key
            self._users += 1
            model = self._model
        try:
            yield model
        finally:
            with self._lock:
                self._users -= 1
                if self._users == 0 and not settings.dolphin_keep_loaded:
                    self._unload_locked()

    def _unload_locked(self) -> None:
        model, self._model, self._key = self._model, None, None
        if model is not None:
            model.release()
        del model
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()


model_manager = _ModelManager()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def process_elements_selective(
    layout_results: str,
    image: Image.Image,
    model: DOLPHIN,
    max_batch_size: int,
    recognize_labels: Collection[str],
) -> list[dict]:
    """Upstream ``process_elements`` that only reads the text of some labels.

    Every element keeps its layout (label, bbox, tags, reading order); those
    whose label is not in ``recognize_labels`` get ``text == ""``.
    """
    layout_list = parse_layout_string(layout_results)
    if not layout_list or not (layout_results.startswith("[") and layout_results.endswith("]")):
        layout_list = [([0, 0, *image.size], "distorted_page", [])]
    elif len(layout_list) > 1 and check_bbox_overlap(layout_list, image):
        logger.info("High bbox overlap: treating the page as a distorted page")
        layout_list = [([0, 0, *image.size], "distorted_page", [])]

    results: list[dict] = []
    pending: dict[str, list[dict]] = {}
    reading_order = 0
    for bbox, label, tags in layout_list:
        try:
            if label == "distorted_page":
                x1, y1, x2, y2 = 0, 0, *image.size
            else:
                x1, y1, x2, y2 = process_coordinates(bbox, image)
            if x2 - x1 > 3 and y2 - y1 > 3:
                info = {"label": label, "bbox": [x1, y1, x2, y2], "reading_order": reading_order, "tags": tags}
                if label in recognize_labels and label != "fig":
                    crop = image.crop((x1, y1, x2, y2))
                    pending.setdefault(LABEL_PROMPTS.get(label, TEXT_PROMPT), []).append({**info, "crop": crop})
                else:
                    results.append({**info, "text": ""})
            reading_order += 1
        except Exception as e:  # same policy as upstream: skip the broken element
            logger.warning("Error processing bbox with label %s: %s", label, e)

    for prompt, elements in pending.items():
        results.extend(process_element_batch(elements, model, prompt, max_batch_size))
    results.sort(key=lambda x: x.get("reading_order", 0))
    return results


def parse_pages(
    images: list[Image.Image],
    out_dir: str | Path,
    *,
    settings: Settings | None = None,
    recognize_labels: Collection[str] | None = None,
    ocr_pages: Collection[int] | None = None,
    on_page_done: Callable[[], None] | None = None,
) -> dict:
    """Run Dolphin-v2 on rendered pages and write ``out_dir/recognition.json``.

    Args:
        recognize_labels: labels whose text is read; ``None`` reads every
            element like upstream.  ``DOLPHIN_RECOGNIZE_LABELS`` overrides it
            ("all" or a comma-separated list).
        ocr_pages: indices of the pages whose text is read (``None``: all);
            on the others only the layout is analysed.  Ignored when
            ``DOLPHIN_RECOGNIZE_LABELS`` is set.
        on_page_done: called after each page (e.g. to advance a progress bar).

    Returns:
        ``{"total_pages": n, "pages": [{"page_number", "elements": [...]}]}``
        with bounding boxes in the pixel coordinates of ``images``.
    """
    settings = settings or Settings.from_env()
    override = settings.dolphin_recognize_labels
    if override:
        recognize_labels = None if override.lower() == "all" else {s.strip() for s in override.split(",") if s.strip()}
        ocr_pages = None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    if recognize_labels is None:  # upstream saves figure crops here
        (out_dir / "markdown" / "figures").mkdir(parents=True, exist_ok=True)

    pages = []
    with model_manager.use(settings) as model:
        for index, image in enumerate(images):
            layout = model.chat(LAYOUT_PROMPT, image)
            labels = recognize_labels if ocr_pages is None or index in ocr_pages else ()
            if labels is None:
                elements = process_elements(
                    layout, image, model, settings.dolphin_max_batch_size, str(out_dir), f"page_{index + 1:03d}"
                )
            else:
                elements = process_elements_selective(layout, image, model, settings.dolphin_max_batch_size, labels)
            pages.append({"page_number": index + 1, "elements": elements})
            if on_page_done is not None:
                on_page_done()
        del model

    recognition = {"total_pages": len(pages), "pages": pages}
    (out_dir / "recognition.json").write_text(json.dumps(recognition, ensure_ascii=False, indent=1), encoding="utf-8")
    return recognition
