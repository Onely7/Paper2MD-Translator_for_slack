"""The examples in the docstrings of paper2md (modules that do not load Dolphin/torch)."""

import doctest
import importlib

MODULES = [
    "cache",
    "glossary",
    "layout",
    "llm",
    "markdown",
    "models",
    "options",
    "pdf",
    "polish",
    "prompts",
    "sources",
    "terms",
    "textlayer",
    "translation",
]
OTHER_MODULES = ["bot.messages"]


def load_tests(loader, tests, ignore):
    for name in [f"paper2md.{name}" for name in MODULES] + OTHER_MODULES:
        tests.addTests(doctest.DocTestSuite(importlib.import_module(name)))
    return tests
