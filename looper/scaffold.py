"""``looper --init``: write a minimal, working config for a new user.

The shipped ``config.yaml`` carries 73 keys because it documents every knob.
That is the right reference file and the wrong *first* file: a new user has to
read all of it to discover that eight keys matter. This module writes those
eight, with the rest left to the validated defaults in
:mod:`looper.config`, so the generated file is short enough to read in full
and still passes ``--check-config`` unmodified.

Two rules, both learned the hard way:

* **Never clobber.** The scaffold refuses to overwrite an existing file. The
  tracked 194-line config in this very repo was once destroyed by a stub
  write; a scaffold that can do that to a user's tuned config is a footgun,
  not a convenience.
* **No secret-shaped placeholders.** The key is referenced by *environment
  variable name*, never inlined, so the generated file is safe to commit and
  cannot trip a repo's committed-secret scanner.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("looper.scaffold")

#: Filename written when the user does not name one.
DEFAULT_CONFIG_NAME = "config.yaml"

STARTER_CONFIG = """\
# Looper starter config -- the eight keys that matter.
# Every other key falls back to a validated default; see docs/configuration.md
# for the full reference, or the repo's own config.yaml for a documented copy.
#
# Nothing here is a secret: the OpenRouter key is read from the environment
# variable named below, never from this file. This file is safe to commit.

workspace: ./workspace

execution:
  # How many build->test->review->fix cycles before giving up.
  max_cycles: 5
  # Stop early at this score; refuse to call the build acceptable below the
  # minimum. The gate is fail-closed: unverified evidence scores zero.
  target_score: 99
  min_acceptable: 95
  # HARD ceiling in USD for one build. 0 disables it -- set a real number
  # before your first paid run. The budget is reserved before each call, so
  # this is a ceiling and not a post-hoc report.
  max_cost_usd: 5.0
  # Refuse to run LLM-authored tests when no isolation backend exists,
  # instead of silently executing them on this host. Run `looper --doctor`
  # to see what isolation this machine can actually provide.
  sandbox_fail_closed: true

openrouter:
  # The key is read from this environment variable at startup.
  api_key_env: OPENROUTER_API_KEY
"""


class ScaffoldExistsError(FileExistsError):
    """Raised when the target config file is already present."""


def write_starter_config(path: Path) -> Path:
    """Write :data:`STARTER_CONFIG` to ``path``, refusing to overwrite.

    Returns the resolved path written. Raises :class:`ScaffoldExistsError`
    when the file exists -- the caller turns that into a config-error exit
    rather than destroying a tuned config.
    """
    target = Path(path)
    if target.exists():
        raise ScaffoldExistsError(
            f"{target} already exists; refusing to overwrite it. "
            "Delete it or pass --config with a different filename."
        )
    # ``Path("config.yaml").parent`` is ``Path(".")``, never empty, so the
    # mkdir is unconditional -- guarding it produced a branch no input could
    # take.
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(STARTER_CONFIG, encoding="utf-8")
    return target
