"""
OpenAI models that users can pick, and the reasoning efforts each one accepts.

The catalog follows OpenAI's model pages (November 2026): GPT-6.1 Sol and
GPT-6 Astra do not accept ``none``; GPT-6 Luna and GPT-6.1 Terra accept every
level.  Prices are USD per 1M tokens of the standard tier (``usage.py`` prices
the requests with them).

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
class Prices:
    """USD per 1M tokens (standard tier, up to 272K input tokens)."""

    input: float
    cached_input: float  # read from the prompt cache
    cache_write: float  # written to the prompt cache (30-minute retention: 1.25x input)
    output: float  # reasoning tokens included

    @property
    def text(self) -> str:
        """
        >>> Prices(0.1, 0.01, 0.125, 0.5).text
        '$0.1 / $0.5'
        """
        return f"${self.input:g} / ${self.output:g}"


@dataclass(frozen=True)
class ModelSpec:
    id: str
    name: str
    prices: Prices | None = None  # None: unknown (not priced)
    efforts: tuple[str, ...] = EFFORT_LEVELS  # empty: not a reasoning model

    @property
    def price(self) -> str:
        """ "$1 / $5": input / output per 1M tokens, shown in the Home tab."""
        return self.prices.text if self.prices else ""

    @property
    def label(self) -> str:
        return f"{self.name}（{self.price}）" if self.price else self.name


CATALOG = (
    ModelSpec("gpt-6-luna", "GPT-6 Luna", Prices(0.1, 0.01, 0.125, 0.5)),
    ModelSpec("gpt-6.1-terra", "GPT-6.1 Terra", Prices(1, 0.05, 1.25, 5)),
    ModelSpec("gpt-6.1-sol", "GPT-6.1 Sol", Prices(2, 0.1, 2.5, 10), _WITHOUT_NONE),
    ModelSpec("gpt-6-astra", "GPT-6 Astra", Prices(10, 1, 12.5, 50), _WITHOUT_NONE),
)
DEFAULT_MODEL_CHOICES = tuple(spec.id for spec in CATALOG)

# The tasks a model is chosen for, in the order they run.
TASKS = ("glossary", "translation", "polish", "summary")
TASK_NAMES = {"glossary": "訳語表", "translation": "翻訳", "polish": "推敲", "summary": "要約"}
# The two yomiyasu polishing passes, each switched on or off: every block (paragraph, caption, ...) on its
# own, and the whole Markdown.  The block pass sends many requests, so it is off by default.
POLISH_SCOPES = {"polish_segments": "ブロックごと", "polish_document": "Markdown 全体"}


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
    return ModelSpec(model, model, None, EFFORT_LEVELS if is_reasoning_model(model) else ())


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
PREFERENCE_NAMES = (*(f"{task}_{field}" for task in TASKS for field in ("model", "effort")), *POLISH_SCOPES)


def _switch(value: str | None, default: bool) -> bool:
    """A saved on/off preference ("on" / "off"), or ``default``."""
    return default if value not in ("on", "off") else value == "on"


@dataclass(frozen=True)
class ModelChoice:
    """Models and efforts for one translation (a user's saved choice, else the server defaults)."""

    translation_model: str
    translation_effort: str | None
    summary_model: str
    summary_effort: str | None
    polish_model: str = ""
    polish_effort: str | None = None
    polish_segments: bool = False  # yomiyasu polishing of every block
    polish_document: bool = False  # yomiyasu polishing of the whole Markdown
    glossary_model: str = ""  # "": no glossary is made (or, for a stored result, not known)
    glossary_effort: str | None = None

    @property
    def polishes(self) -> bool:
        return self.polish_segments or self.polish_document

    def normalized(self) -> ModelChoice:
        """The same choice, without a polish model when nothing is polished (for comparing choices)."""
        return self if self.polishes else replace(self, polish_model="", polish_effort=None)

    @classmethod
    def resolve(cls, saved: Mapping[str, str] | None, settings: Settings) -> ModelChoice:
        """Saved values that are still valid; the server defaults (``settings``) for the rest."""
        saved = saved or {}
        values: dict = {}
        for task in TASKS:
            if task == "glossary" and not settings.glossary:
                continue  # no glossary is made: nothing to choose
            default_model = getattr(settings, f"{task}_model")
            model = saved.get(f"{task}_model") or default_model
            if model not in settings.model_choices and model != default_model:
                model = default_model  # no longer offered
            effort = saved.get(f"{task}_effort") or getattr(settings, f"{task}_effort")
            values[f"{task}_model"] = model
            values[f"{task}_effort"] = coerce_effort(model, effort)
        for name in POLISH_SCOPES:
            values[name] = _switch(saved.get(name), getattr(settings, name))
        return cls(**values)

    def apply(self, settings: Settings) -> Settings:
        tasks = [task for task in TASKS if task != "glossary" or self.glossary_model]
        names = [f"{task}_{field}" for task in tasks for field in ("model", "effort")] + list(POLISH_SCOPES)
        return replace(settings, **{name: getattr(self, name) for name in names})

    def lines(self) -> list[str]:
        """
        One line per task, in the order they run.

        >>> ModelChoice("gpt-6-luna", "medium", "gpt-6.1-sol", "medium", "gpt-6.1-terra", "low", False, True).lines()
        ['翻訳 `gpt-6-luna`（effort: medium）', '推敲 `gpt-6.1-terra`（effort: low、Markdown 全体）', '要約 `gpt-6.1-sol`（effort: medium）']
        >>> ModelChoice("a", None, "b", None, "gpt-6.1-terra", "low", True, True).lines()[1]
        '推敲 `gpt-6.1-terra`（effort: low、ブロックごと＋Markdown 全体）'
        >>> ModelChoice("gpt-6.1-terra", "none", "gpt-6.1-sol", "medium").lines()[1]
        '推敲 なし'
        >>> ModelChoice("a", None, "b", None, glossary_model="gpt-6.1-terra", glossary_effort="low").lines()[0]
        '訳語表 `gpt-6.1-terra`（effort: low）'
        """
        out = []
        for task in TASKS:
            model, effort = getattr(self, f"{task}_model"), getattr(self, f"{task}_effort")
            if task == "glossary" and not model:
                continue
            notes = [f"effort: {effort}"] if effort else []
            if task == "polish":
                if not self.polishes:
                    out.append(f"{TASK_NAMES[task]} なし")
                    continue
                notes.append("＋".join(label for name, label in POLISH_SCOPES.items() if getattr(self, name)))
            out.append(f"{TASK_NAMES[task]} `{model}`" + (f"（{'、'.join(notes)}）" if notes else ""))
        return out

    def describe(self) -> str:
        """
        >>> ModelChoice("gpt-6.1-terra", "none", "gpt-6.1-sol", "medium").describe()
        '翻訳 `gpt-6.1-terra`（effort: none）、推敲 なし、要約 `gpt-6.1-sol`（effort: medium）'
        """
        return "、".join(self.lines())


def offered_models(settings: Settings) -> list[ModelSpec]:
    """The models shown in the Home tab: ``OPENAI_MODEL_CHOICES`` plus the server defaults."""
    defaults = [getattr(settings, f"{task}_model") for task in TASKS]
    return [model_spec(model) for model in dict.fromkeys([*settings.model_choices, *defaults])]
