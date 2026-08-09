"""Offline stub LLM client: prove the gate without an API key or a bill.

The single biggest adoption barrier measured in the head-to-head audit was
that *nothing* about Looper could be observed before wiring up an OpenRouter
key: the first thing a new user saw was either a config error or a bill. That
is backwards for a tool whose whole value proposition is its refusals -- the
refusals are exactly what a prospective user needs to watch happen.

:class:`StubClient` answers every agent call locally with a canned, *honest*
artifact: real Python that really parses, a real pytest suite that really
imports the generated module and would really run. Nothing else in the
pipeline is stubbed. The lint gate, the adequacy gate, the sandbox, the
scoring engine and the fail-closed exits all behave exactly as they do in a
paid run -- so a dry run on a host with no sandbox correctly loses the test
weight and says why, rather than pretending the build passed. A dry run that
faked a 99 would be worse than no dry run at all.

Spend is genuinely zero: no request is issued, so no cost is accumulated and
``running_cost_usd()`` stays at 0.0 for the whole run.
"""

from __future__ import annotations

import logging

from looper.config import AgentSpec, LooperConfig
from looper.llm import AgentReply, OpenRouterClient, TokenUsage

logger = logging.getLogger("looper.dryrun")

#: The module the stub builder emits. The stub test suite imports this name,
#: which is also what ``_subject_modules()`` derives from ``src/``, so the
#: adequacy gate's "is this suite connected to the artifact?" check passes on
#: genuine evidence rather than on a special case.
STUB_MODULE = "generated_code"

#: A dry run spends nothing. Exported so a test can assert the claim rather
#: than trusting the docstring.
STUB_PRICE_USD = 0.0

STUB_CODE = '''"""A tiny, complete module produced by the offline dry run."""


class Cart:
    """A shopping cart with a running total."""

    def __init__(self) -> None:
        self.items: dict[str, float] = {}

    def add(self, name: str, price: float) -> None:
        """Add one item. Prices must be non-negative."""
        if price < 0:
            raise ValueError("price must be non-negative")
        self.items[name] = self.items.get(name, 0.0) + price

    def remove(self, name: str) -> None:
        """Remove an item, raising KeyError when it is absent."""
        del self.items[name]

    @property
    def total(self) -> float:
        """Sum of every item currently in the cart."""
        return round(sum(self.items.values()), 2)
'''

STUB_TESTS = f'''"""Offline dry-run test suite for the stub artifact."""

import pytest

from {STUB_MODULE} import Cart


def test_empty_cart_totals_zero():
    cart = Cart()
    assert cart.total == 0.0
    assert cart.items == {{}}


def test_add_accumulates():
    cart = Cart()
    cart.add("apple", 1.5)
    cart.add("apple", 2.0)
    assert cart.total == 3.5
    assert len(cart.items) == 1


def test_negative_price_rejected():
    cart = Cart()
    with pytest.raises(ValueError):
        cart.add("apple", -1.0)
    assert cart.total == 0.0


def test_remove_missing_item_raises():
    cart = Cart()
    with pytest.raises(KeyError):
        cart.remove("ghost")
'''

STUB_REVIEW = """# Offline dry-run review

The artifact is small, typed, and validates its inputs. Error paths raise
rather than returning sentinels, and the public surface is four members.

- LOW: `total` recomputes on every access; fine at this size.

Score: 97
"""

STUB_SECURITY = """# Offline dry-run security audit

The module performs no I/O, spawns no processes, and parses no untrusted
input formats.

No issues found.
"""

STUB_PROSE = """# Offline dry-run artifact

This document was produced by `--dry-run`, which answers every agent locally
so the pipeline can be observed end to end without an API key or any spend.
Replace it with a real run once a key is configured.
"""

#: Agent key -> canned reply. Every key in ``DEFAULT_AGENTS`` is covered; an
#: unrecognised key falls back to prose, so adding an agent cannot break the
#: dry run.
_STUB_REPLIES: dict[str, str] = {
    "builder": STUB_CODE,
    "fixer": STUB_CODE,
    "tester": STUB_TESTS,
    "reviewer": STUB_REVIEW,
    "security_auditor": STUB_SECURITY,
    "performance_optimizer": STUB_CODE,
}


class StubClient(OpenRouterClient):
    """An :class:`OpenRouterClient` that never leaves the machine.

    Subclassed rather than duck-typed so the daemon's type contract is
    unchanged and every accessor the orchestrator reads
    (``total_usage``, ``call_count``, ``running_cost_usd``,
    ``cost_by_model``) keeps working untouched.
    """

    def __init__(self, config: LooperConfig) -> None:
        # ``client`` is supplied so the constructor never builds an SDK object
        # or complains about a missing key: a dry run must work with no
        # credentials at all, which is the entire point.
        super().__init__(config.openrouter, config.retry, client=object())
        self._role_to_key = {spec.role: key for key, spec in config.agents.items()}

    async def call(
        self,
        agent: AgentSpec,
        prompt: str,
        *,
        extra_system: str = "",
    ) -> AgentReply:
        """Answer locally. No request, no retry, no cost."""
        key = self._role_to_key.get(agent.role, "")
        text = _STUB_REPLIES.get(key, STUB_PROSE)
        self.call_count += 1
        logger.info("[dry-run] %s answered locally (%d chars, $0.00)", agent.role, len(text))
        return AgentReply(text=text, ok=True, attempts=1, usage=TokenUsage())
