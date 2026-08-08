"""Verify configured model slugs against the live OpenRouter catalogue.

A wrong slug is the most expensive kind of config error in this system: it
passes ``--check-config`` (it is a perfectly well-formed string), then fails
at build time, mid-pipeline, after earlier phases have already been paid for.
The catalogue is a public unauthenticated endpoint, so checking is cheap.

Kept separate from :mod:`looper.llm` because this is a one-shot diagnostic and
must not drag the OpenAI SDK or a client object into the CLI's import path.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger("looper.models")

#: The catalogue is served from the API root, not the chat-completions path.
MODELS_PATH = "/models"


class CatalogueUnavailableError(RuntimeError):
    """The OpenRouter catalogue could not be fetched.

    Deliberately distinct from "the slug is wrong": an unreachable catalogue
    must never be reported as a bad model, or an offline laptop would look
    like a broken config.
    """


@dataclass(frozen=True, slots=True)
class ModelCheck:
    """Outcome of verifying one agent's model slug."""

    agent: str
    model: str
    known: bool


def fetch_catalogue(base_url: str, *, timeout: float = 30.0) -> frozenset[str]:
    """Return every model id OpenRouter currently serves.

    No API key is sent: the catalogue is public, and this runs before any
    credential is necessarily configured.
    """
    url = f"{base_url.rstrip('/')}{MODELS_PATH}"
    if not url.startswith(("http://", "https://")):  # pragma: no cover - guarded upstream
        raise CatalogueUnavailableError(f"refusing non-http(s) catalogue URL {url!r}")
    request = urllib.request.Request(  # nosec B310 - scheme checked above
        url, headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise CatalogueUnavailableError(f"could not reach {url}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CatalogueUnavailableError(f"{url} returned invalid JSON: {exc}") from exc

    data = payload.get("data")
    if not isinstance(data, list):
        raise CatalogueUnavailableError(f"{url} returned no model list")

    ids = {entry["id"] for entry in data if isinstance(entry, dict) and "id" in entry}
    if not ids:
        raise CatalogueUnavailableError(f"{url} returned an empty model list")
    return frozenset(ids)


def check_models(
    agents: dict[str, str],
    catalogue: frozenset[str],
) -> list[ModelCheck]:
    """Check each ``agent -> model`` pair against ``catalogue``.

    Returned in a stable order so the CLI output and its tests are
    deterministic.
    """
    return [
        ModelCheck(agent=agent, model=model, known=model in catalogue)
        for agent, model in sorted(agents.items())
    ]
