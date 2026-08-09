"""Language adapters: what "lint it, test it, count its assertions" means.

Every gate in Looper was implicitly Python-only. ``lint_generated`` accepted
``py_compile|flake8``, the build prompt hard-coded a ```` ```python ```` fence,
syntax verification called :func:`ast.parse`, and the adequacy gate counted
Python AST nodes. None of that is wrong -- it is simply *one* instance of a
general contract that was never named, so a TypeScript or Go artifact could
not be gated at all.

This module names the contract. An adapter answers four questions about a
generated artifact:

* what file extension and fence tag does its language use?
* does this source parse? (cheap, in-process, no subprocess)
* what argv lints a file on disk?
* what argv runs its test suite, and how is that runner's output parsed?

Python is implemented here because it is the language the pipeline has always
supported, and implementing it *through the adapter* rather than beside it is
what proves the abstraction carries real weight rather than being a wrapper
around a single hard-coded path.

Adding a language is then a data exercise, not a surgery on five modules --
but note honestly what that does **not** give you: the adequacy gate's
assertion counting is still Python-AST based, so a non-Python adapter gets
syntax, lint and test execution, and falls back to the density floor alone
for adequacy. That gap is documented rather than papered over.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass
from typing import Protocol, Sequence


class LanguageAdapter(Protocol):
    """What the pipeline needs to know about one target language."""

    #: Short identifier used in config (``execution.language``).
    @property
    def name(self) -> str:
        """Short identifier used in config (``execution.language``)."""
        raise NotImplementedError  # pragma: no cover - protocol

    @property
    def extension(self) -> str:
        """Extension for the primary generated artifact, including the dot."""
        raise NotImplementedError  # pragma: no cover - protocol

    @property
    def fence_tag(self) -> str:
        """Markdown fence tag the builder is told to emit."""
        raise NotImplementedError  # pragma: no cover - protocol

    def parse_ok(self, source: str) -> tuple[bool, str]:
        """(parses, note). Must not spawn a subprocess."""
        raise NotImplementedError  # pragma: no cover - protocol

    def lint_argv(self, path: str, mode: str) -> list[str]:
        """Argv that lints ``path``. Empty list == linting disabled."""
        raise NotImplementedError  # pragma: no cover - protocol

    def lint_modes(self) -> tuple[str, ...]:
        """Accepted values for ``execution.lint_generated``."""
        raise NotImplementedError  # pragma: no cover - protocol

    def test_argv(self, tests_dir: str) -> list[str]:
        """Argv that runs the suite in ``tests_dir``."""
        raise NotImplementedError  # pragma: no cover - protocol


@dataclass(frozen=True, slots=True)
class PythonAdapter:
    """The original behaviour, expressed through the adapter contract.

    Every argv here is byte-for-byte what the pipeline used before the
    abstraction existed -- including ``-E -s -B`` rather than ``-I`` on the
    pytest launcher, because ``-I`` implies ``-P`` and drops the script
    directory from ``sys.path``, which made every suite that imported the
    module under test fail collection.
    """

    name: str = "python"
    extension: str = ".py"
    fence_tag: str = "python"

    def parse_ok(self, source: str) -> tuple[bool, str]:
        if not source.strip():
            return False, "build produced empty output"
        try:
            ast.parse(source)
        except SyntaxError as exc:
            return False, f"generated code has a syntax error: {exc}"
        return True, ""

    def lint_modes(self) -> tuple[str, ...]:
        return ("off", "py_compile", "flake8")

    def lint_argv(self, path: str, mode: str) -> list[str]:
        if mode == "off":
            return []
        if mode == "py_compile":
            return [sys.executable, "-m", "py_compile", path]
        return [sys.executable, "-m", "flake8", "--max-line-length=100", path]

    def test_argv(self, tests_dir: str) -> list[str]:
        return [
            sys.executable,
            "-E",
            "-s",
            "-B",
            "-m",
            "pytest",
            tests_dir,
            "-q",
            "-p",
            "no:cacheprovider",
            "--no-header",
        ]


#: Registry. Keyed by ``execution.language``; Python is the default so an
#: existing config keeps its exact behaviour without naming a language.
ADAPTERS: dict[str, LanguageAdapter] = {"python": PythonAdapter()}

DEFAULT_LANGUAGE = "python"


def supported_languages() -> tuple[str, ...]:
    return tuple(sorted(ADAPTERS))


def adapter_for(language: str) -> LanguageAdapter:
    """Look up an adapter, raising ``KeyError`` for an unknown language.

    The config validator checks the name up-front, so reaching this with a
    bad value means a caller bypassed validation -- which should be loud.
    """
    return ADAPTERS[language]


def lint_modes_for(language: str) -> Sequence[str]:
    return adapter_for(language).lint_modes()
