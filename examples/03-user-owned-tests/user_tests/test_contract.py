"""Human-owned acceptance tests. NO agent ever sees this file.

This is the contract the generated artifact must satisfy. Write these BEFORE
running the build, and write them against behaviour you actually care about --
edge cases, error handling, and the boundaries an LLM tends to skip.

Import the artifact the way the build emits it (single_file mode):
    workspace/cart/src/generated_code.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ARTIFACT = Path(__file__).resolve().parents[1] / "workspace" / "cart" / "src" / "generated_code.py"


def _load():
    if not ARTIFACT.exists():  # pragma: no cover - guard for a pre-build run
        pytest.skip(f"artifact not built yet: {ARTIFACT}")
    spec = importlib.util.spec_from_file_location("generated_code", ARTIFACT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["generated_code"] = module
    spec.loader.exec_module(module)
    return module


def test_module_exposes_a_cart():
    module = _load()
    assert hasattr(module, "Cart"), "the artifact must expose a Cart class"


def test_total_of_an_empty_cart_is_zero():
    cart = _load().Cart()
    assert cart.total() == 0


def test_add_then_total():
    cart = _load().Cart()
    cart.add("apple", price=2.50, quantity=2)
    assert cart.total() == pytest.approx(5.00)


def test_remove_is_idempotent_for_missing_items():
    """An edge case LLMs routinely skip: removing what was never added."""
    cart = _load().Cart()
    cart.remove("ghost")  # must not raise
    assert cart.total() == 0


def test_negative_quantity_is_rejected():
    """The error path matters more than the happy path."""
    cart = _load().Cart()
    with pytest.raises((ValueError, AssertionError)):
        cart.add("apple", price=2.50, quantity=-1)
