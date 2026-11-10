"""The examples in the docstrings of paper2md (modules that do not load Dolphin/torch)."""

import doctest
import importlib

MODULES = ["cache", "layout", "llm", "markdown", "models", "options", "pdf", "prompts", "sources", "textlayer"]


def load_tests(loader, tests, ignore):
    for name in MODULES:
        tests.addTests(doctest.DocTestSuite(importlib.import_module(f"paper2md.{name}")))
    return tests
