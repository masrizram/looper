# Looper in a container: the sandbox problem and the install problem have
# the same answer.
#
# On a bare host, `looper --doctor` exits 5 unless Docker, Podman, POSIX
# rlimits or WSL can isolate LLM-written test code -- which on Windows and
# macOS means a new user's very first command is a refusal. Running looper
# itself inside a Linux container makes POSIX rlimits available in-process,
# so the fail-closed sandbox has a backend without the user installing
# anything beyond the container runtime they already used to get here.
FROM python:3.11-slim AS base

# Deliberately NOT root: the sandbox drops privileges for generated tests,
# and a container that starts as root undercuts that on any backend that
# inherits the caller's uid.
RUN useradd --create-home --uid 10001 looper

WORKDIR /app

# Dependencies first so a source edit does not invalidate the wheel cache.
COPY requirements.txt ./
RUN pip install --no-cache-dir --require-hashes=false -r requirements.txt

COPY pyproject.toml README.md ./
COPY looper ./looper
COPY daemon.py ./daemon.py
RUN pip install --no-cache-dir --no-deps .

# The workspace is a volume mount point: build artifacts and the git trail
# must outlive the container, or every run starts from nothing.
RUN mkdir -p /work && chown looper:looper /work
USER looper
WORKDIR /work

# No API key is baked in and none is required to see the gate work:
#   docker run --rm ghcr.io/masrizram/looper --dry-run --goal "a CLI todo app"
ENTRYPOINT ["looper"]
CMD ["--help"]
