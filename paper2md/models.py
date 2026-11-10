"""
OpenAI models that users can pick, and the reasoning efforts each one accepts.

The catalog follows OpenAI's model pages (November 2026): GPT-6.1 Sol and
GPT-6 Astra do not accept ``none``; GPT-6 Luna and GPT-6.1 Terra accept every
level.  Prices are USD per 1M input / output tokens.

Which models are offered is set with ``OPENAI_MODEL_CHOICES`` (comma separated
IDs, default: the catalog).  A model outside the catalog is shown with its ID;
if it is a reasoning model every effort is offered, and ``llm.OpenAIClient``
falls back to an accepted value when the API rejects one.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Settings

EFFORT_LEVELS = ("none", "low", "medium", "high", "xhigh", "max")
_WITHOUT_NONE = EFFORT_LEVELS[1:]


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    price: str = ""  # "$1 / $5": input / output per 1M tokens
    efforts: tuple[str, ...] = EFFORT_LEVELS  # empty: not a reasoning model

    @property
    def label(self) -> str:
        return f"{self.name}（{self.price}）" if self.price else self.name


CATALOG = (
    ModelSpec("gpt-6-luna", "GPT-6 Luna", "$0.1 / $0.5"),
    ModelSpec("gpt-6.1-terra", "GPT-6.1 Terra", "$1 / $5"),
    ModelSpec("gpt-6.1-sol", "GPT-6.1 Sol", "$2 / $10", _WITHOUT_NONE),
    ModelSpec("gpt-6-astra", "GPT-6 Astra", "$10 / $50", _WITHOUT_NONE),
)
DEFAULT_MODEL_CHOICES = tuple(spec.id for spec in CATALOG)


# ---------------------------------------------------------------------------
# Model families
# ---------------------------------------------------------------------------
_GPT_VERSION_RE = re.compile(r"^(?:ft:)?gpt-(\d+)(?:\.(\d+))?(?![\d.])")


def parse_gpt_version(model: str) -> tuple[int, int] | None:
    """
    >>> parse_gpt_version("gpt-6.1-terra")
    (6, 1)
    >>> parse_gpt_version("gpt-5-mini")
    (5, 0)
    >>> parse_gpt_version("o3") is None
    True
    """
    match = _GPT_VERSION_RE.match(model.lower())
    return (int(match.group(1)), int(match.group(2) or 0)) if match else None


def is_reasoning_model(model: str) -> bool:
    """
    >>> [is_reasoning_model(m) for m in ("gpt-6.1-sol", "gpt-5.4-mini", "o4-mini", "gpt-4.1", "gpt-5-chat-latest")]
    [True, True, True, False, False]
    """
    name = model.lower()
    if "chat-latest" in name:
        return False
    version = parse_gpt_version(name)
    if version is not None:
        return version[0] >= 5
    return bool(re.search(r"(^|[^a-z0-9])o\d+([^a-z0-9]|$)", name))  # o1, o3, o4-mini, ...


def model_spec(model: str) -> ModelSpec:
    """The catalog entry of ``model``, or a generic one for other models."""
    for spec in CATALOG:
        if spec.id == model:
            return spec
    return ModelSpec(model, model, "", EFFORT_LEVELS if is_reasoning_model(model) else ())


def coerce_effort(model: str, effort: str | None) -> str | None:
    """
    The accepted effort closest to ``effort`` (ties go to the lower one); None for non-reasoning models.

    >>> coerce_effort("gpt-6.1-sol", "none")
    'low'
    >>> coerce_effort("gpt-6.1-terra", "none")
    'none'
    >>> coerce_effort("gpt-6-astra", "minimal")
    'low'
    >>> coerce_effort("gpt-4.1", "high") is None
    True
    """
    efforts = model_spec(model).efforts
    if not efforts:
        return None
    if effort == "minimal":
        effort = "low"
    if effort in efforts:
        return effort
    rank = EFFORT_LEVELS.index(effort) if effort in EFFORT_LEVELS else EFFORT_LEVELS.index("medium")
    return min(efforts, key=lambda e: (abs(EFFORT_LEVELS.index(e) - rank), EFFORT_LEVELS.index(e)))


def lowest_effort(model: str) -> str | None:
    """The cheapest effort the model accepts (for copying text, which needs no reasoning)."""
    efforts = model_spec(model).efforts
    return efforts[0] if efforts else None


# ---------------------------------------------------------------------------
# A user's choice
# ---------------------------------------------------------------------------
PREFERENCE_NAMES = ("translation_model", "translation_effort", "summary_model", "summary_effort")


@dataclass(frozen=True)
class ModelChoice:
    """Models and efforts for one translation (a user's saved choice, else the server defaults)."""

    translation_model: str
    translation_effort: str | None
    summary_model: str
    summary_effort: str | None

    @classmethod
    def resolve(cls, saved: Mapping[str, str] | None, settings: Settings) -> ModelChoice:
        """Saved values that are still valid; the server defaults (``settings``) for the rest."""
        saved = saved or {}
        values = {}
        for task in ("translation", "summary"):
            default_model = getattr(settings, f"{task}_model")
            model = saved.get(f"{task}_model") or default_model
            if model not in settings.model_choices and model != default_model:
                model = default_model  # no longer offered
            effort = saved.get(f"{task}_effort") or getattr(settings, f"{task}_effort")
            values[f"{task}_model"] = model
            values[f"{task}_effort"] = coerce_effort(model, effort)
        return cls(**values)

    def apply(self, settings: Settings) -> Settings:
        return replace(
            settings,
            translation_model=self.translation_model,
            translation_effort=self.translation_effort,
            summary_model=self.summary_model,
            summary_effort=self.summary_effort,
        )

    def describe(self) -> str:
        """
        >>> ModelChoice("gpt-6.1-terra", "none", "gpt-6.1-sol", "medium").describe()
        '翻訳 `gpt-6.1-terra`（effort: none）、要約 `gpt-6.1-sol`（effort: medium）'
        """

        def one(model: str, effort: str | None) -> str:
            return f"`{model}`（effort: {effort}）" if effort else f"`{model}`"

        return (
            f"翻訳 {one(self.translation_model, self.translation_effort)}、"
            f"要約 {one(self.summary_model, self.summary_effort)}"
        )


def offered_models(settings: Settings) -> list[ModelSpec]:
    """The models shown in the Home tab: ``OPENAI_MODEL_CHOICES`` plus the server defaults."""
    ids = list(dict.fromkeys([*settings.model_choices, settings.translation_model, settings.summary_model]))
    return [model_spec(model) for model in ids]
