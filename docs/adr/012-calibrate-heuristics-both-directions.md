# ADR-012: Calibrate heuristics in both directions

## Status

Accepted.

## Context

The fourth audit round found six defects in code that was at 100% line and
branch coverage, passing black, isort, flake8, mypy --strict, bandit and
pip-audit, and that two previous audit rounds had already been through.

Every one of them was the same mistake:

* the sandbox scanner refused `tmp_path.write_text(...)` -- the single most
  common filesystem idiom in pytest -- so no build whose tests touched disk
  could ever clear the gate;
* it also refused a docstring that merely *mentioned* `socket.`, while the
  substring pass it protected added nothing the AST pass did not already
  catch;
* the cost ceiling was checked between cycles, but a cycle issues seven agent
  calls, so a $1.00 budget reached $18.00 before the guard was consulted;
* `REVIEW_SCORE_RE` matched the `3` in "Score 3 major problems remain";
* the adequacy gate counted only `ast.Assert`, rating a thorough
  `unittest.TestCase` suite at 0.0 assertions/100 lines while passing
  `assert 1 == 1` repeated three times;
* `-I` on the pytest argv implied `-P`, stripping the workspace from
  `sys.path`, so every suite that imported the module under test -- the one
  thing the test prompt demands -- failed collection with
  `ModuleNotFoundError` and scored zero.

Coverage could not have caught any of these. Coverage proves a line executed;
it says nothing about whether the threshold on that line is correct. Worse,
every fake in the test suite behaved identically -- the scanned suite was
always clean or always hostile, the agent always returned valid code -- so the
tests exercised one shape of input over and over at 100% coverage.

## Decision

**A heuristic is not tested until it is tested in both directions.**

Concretely:

1. Every refusal rule ships with a paired test: one proving the hostile form is
   refused, one proving the legitimate form is accepted. A rule with only the
   first half is treated as incomplete, not as "strict".
2. Scanners return a `ScanVerdict(refuse, warn)` rather than a flat list of
   reasons. "Definitely hostile" and "could conceivably touch something" are
   different questions and must not share one outcome, or every widening of a
   pattern table risks blocking real code.
3. Budget ceilings are enforced at the point of spend (`OpenRouterClient.call`),
   not at a loop boundary. A guard that runs less often than the thing it
   guards is not a ceiling.
4. Parsers and the prompts that feed them change together. `REVIEW_SCORE_RE`
   and `PromptGenerator.review` are one contract in two files.
5. A two-way calibration corpus (`tests/test_audit_v4_regressions.py`) holds
   ten benign suites that must pass and twenty hostile payloads that must be
   refused, run as one parametrized sweep. This is the executable
   specification of the scanner's behaviour; adding a pattern without
   consulting the benign column is how the `tmp_path` bug happened.
6. Test fixtures must survive the product's own gates. The default fake reply
   was `def test_x(): assert True` -- precisely the tautology the adequacy gate
   exists to reject. Fixtures that cannot pass the gates test the wrong
   product, and they hid the `-I`/`sys.path` bug for the entire life of the
   repository.

## Consequences

* False positives are now treated as defects of equal severity to misses. This
  is a deliberate shift: for a release gate, a rule that blocks good builds
  destroys the tool's usefulness just as surely as a rule that admits bad ones.
* `getattr` is no longer flagged blanket-wide, but `getattr(os, "system")` is
  still refused -- the receiver, not the literalness of the attribute name, is
  what matters.
* The container backend gained `--ulimit cpu=`, because `--cpus` is a scheduler
  share and never actually stops a runaway loop. The documentation previously
  claimed a CPU cap the flag did not provide.
* Isolation for the generated test run is `-E -s`, not `-I`. The isolation that
  mattered (no `PYTHON*` env vars, no user site-packages) is retained; the part
  that broke every import is gone.
