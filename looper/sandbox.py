"""Static guardrails for LLM-authored test/code before it runs.

The pipeline executes code written by a language model (``POST /build`` is
RCE by design). A fixed-argv ``subprocess.run`` without ``shell=True`` stops
shell injection, but it does NOT stop the generated Python itself from doing
something destructive (``os.remove``), forking, calling the network, or
looping forever. These helpers refuse to run a suite that contains such
patterns, and launch the remaining suite under OS resource limits so a stray
``while True`` or memory hog cannot wedge or OOM the host.

See ADR-005 (verified evidence, no phantom findings) and ADR-006 (sandbox).
"""

from __future__ import annotations

import ast
import logging
import os
import re
import shlex
import subprocess  # nosec B404 - used with a fixed argv, never shell=True
import tokenize
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Callable, Mapping

logger = logging.getLogger("looper.sandbox")

#: Substring fragments whose *presence* (outside comments AND string literals)
#: means "do not run this untrusted blob on the host". Call-style fragments
#: (subprocess.run, socket.socket) require the call; a bare ``import
#: subprocess`` is NOT enough.
#:
#: This table is deliberately a *superset shortlist* of the dotted paths in
#: :data:`DANGEROUS_CALL_PATHS`: it catches the same intent expressed in a form
#: the AST pass cannot resolve (aliased modules, attribute chains built at
#: runtime). Entries duplicated across both tables are intentional defence in
#: depth, and :func:`scan_source` de-duplicates the resulting reasons.
DESTRUCTIVE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("os.system", "shell execution via os.system"),
    ("os.popen", "shell execution via os.popen"),
    ("os.remove", "filesystem deletion via os.remove"),
    ("os.unlink", "filesystem deletion via os.unlink"),
    ("os.rmdir", "filesystem deletion via os.rmdir"),
    ("shutil.rmtree", "recursive filesystem deletion via shutil.rmtree"),
    ("shutil.move", "filesystem move via shutil.move"),
    ("os.rename", "filesystem move via os.rename"),
    ("subprocess.run", "child process spawn via subprocess.run"),
    ("subprocess.Popen", "child process spawn via subprocess.Popen"),
    ("subprocess.call", "child process spawn via subprocess.call"),
    ("socket.", "raw socket / network access"),
    ("requests.", "outbound HTTP via requests"),
    ("urllib.request", "outbound HTTP via urllib"),
    ("httpx.", "outbound HTTP via httpx"),
    ("__import__", "dynamic import via __import__"),
    ("eval(", "dynamic execution via eval"),
    ("exec(", "dynamic execution via exec"),
    ("marshal.loads", "code loading via marshal"),
    ("pickle.loads", "deserialization via pickle"),
    ("os.fork", "process forking"),
    ("os.kill", "signal delivery via os.kill"),
    ("ctypes.", "native code via ctypes"),
)

#: Fully-qualified call targets refused by the AST pass. Matching the dotted
#: path rather than the bare attribute name is what stops ``json.loads`` (a
#: staple of ordinary tests) being reported as "dangerous builtin 'loads'"
#: while ``os.popen`` sailed through untouched.
DANGEROUS_CALL_PATHS: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "breakpoint",
        "os.system",
        "os.popen",
        "os.remove",
        "os.unlink",
        "os.rmdir",
        "os.removedirs",
        "os.rename",
        "os.replace",
        "os.truncate",
        "os.fork",
        "os.forkpty",
        "os.kill",
        "os.killpg",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.spawnv",
        "os.putenv",
        "shutil.rmtree",
        "shutil.move",
        "shutil.copytree",
        "subprocess.run",
        "subprocess.Popen",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.getoutput",
        "pickle.load",
        "pickle.loads",
        "marshal.load",
        "marshal.loads",
        "socket.socket",
        "socket.create_connection",
        "requests.get",
        "requests.post",
        "requests.request",
        "httpx.get",
        "httpx.post",
        "urllib.request.urlopen",
        "ctypes.CDLL",
        "ctypes.WinDLL",
    }
)

#: Method names that write or delete through an object we cannot resolve
#: statically (``Path(x).write_text``, ``open(...).write``). Flagged on the
#: attribute alone because the receiver is unknowable at scan time -- UNLESS
#: the receiver root is a known-isolated pytest fixture (see
#: :data:`SANDBOXED_RECEIVERS`).
DANGEROUS_METHOD_NAMES: frozenset[str] = frozenset(
    {
        "write_text",
        "write_bytes",
        "unlink",
        "rmdir",
        "mkdir",
        "touch",
        "symlink_to",
        "hardlink_to",
        "chmod",
    }
)

#: Receiver roots whose filesystem writes are confined by pytest itself.
#: ``tmp_path``/``tmpdir`` hand out a per-test directory under the pytest
#: basetemp, so ``tmp_path.write_text(...)`` cannot touch anything the suite
#: was not already given. Flagging them refused every idiomatic pytest suite
#: that touches disk, which meant a build whose tests used the single most
#: common fixture in pytest could never clear the gate. Refusing good suites
#: is not "strict" -- it is a false positive with the same cost as a miss.
SANDBOXED_RECEIVERS: frozenset[str] = frozenset(
    {"tmp_path", "tmp_path_factory", "tmpdir", "tmpdir_factory"}
)

#: ``getattr`` is flagged only in its two-argument, non-literal form. The
#: three-argument ``getattr(obj, "name", default)`` and literal-name lookups
#: are ordinary Python; treating every ``getattr`` as an exploit added noise
#: to a tripwire whose whole value is a low false-positive rate.


@dataclass(frozen=True, slots=True)
class ScanVerdict:
    """Outcome of scanning untrusted source, split by what it should cause.

    Two different questions were previously answered by one flat list:
    "is this definitely hostile?" and "could this conceivably touch something?"
    Both caused an outright refusal, so every widening of
    :data:`DANGEROUS_METHOD_NAMES` risked blocking legitimate suites. Now only
    ``refuse`` stops execution; ``warn`` is logged and left to the real
    perimeter (the container/rlimit backend).
    """

    refuse: tuple[str, ...] = ()
    warn: tuple[str, ...] = ()

    @property
    def refused(self) -> bool:
        return bool(self.refuse)


def _strip_comments_and_strings(source: str) -> str:
    """Blank out comments *and* string literals before the substring pass.

    Two bugs lived here. A naive ``line.split("#", 1)[0]`` truncated at a ``#``
    inside a string, so ``url = "http://x/#f"; os.system(cmd)`` scanned clean.
    Stripping only comments then left the opposite hole open in the other
    direction: a docstring reading "never use socket. connections" tripped the
    substring pass and refused a perfectly safe suite. String *contents* can
    never be a call, so they are replaced with an empty literal; the AST pass
    still sees the original source, so nothing real is lost.
    """
    try:
        tokens = list(tokenize.generate_tokens(StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        return source
    kept = [
        token._replace(string='""') if token.type == tokenize.STRING else token
        for token in tokens
        if token.type != tokenize.COMMENT
    ]
    try:
        return tokenize.untokenize(kept)
    except (ValueError, IndentationError):  # pragma: no cover - defensive
        return source


def _import_aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> fully-qualified module path, from every import form.

    Without this the AST pass compared *source text* to a table of dotted
    paths, so two of the three commonest ways to reach ``os.system`` walked
    straight through: ``import os as o; o.system(...)`` and
    ``from os import system; system(...)`` both scanned clean. Resolving the
    name at its binding site is what makes the dotted-path table mean what it
    says.
    """
    table: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                # `import os.path` binds `os`; `import os.path as p` binds `p`.
                table[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                table[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return table


def _dotted_name(func: ast.expr, aliases: Mapping[str, str] | None = None) -> str:
    """Return the dotted call target, e.g. ``os.path.join``, or ``""``.

    ``aliases`` maps locally-bound names back to their real module paths, so
    an aliased or from-imported dangerous call resolves to the same string a
    plain ``os.system`` would.
    """
    parts: list[str] = []
    node: ast.expr | None = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        root = aliases.get(node.id, node.id) if aliases else node.id
        parts.append(root)
        return ".".join(reversed(parts))
    return ""


def _receiver_root(node: ast.expr) -> str:
    """Name at the root of an attribute/subscript/path-join chain.

    ``tmp_path``, ``tmp_path / "d.json"``, ``tmp_path.joinpath("a")["b"]`` all
    resolve to ``"tmp_path"``. A call is followed through its *first argument*
    as well as its target, because the idiomatic helper form
    ``make_file(tmp_path).write_text(...)`` roots the path in the argument,
    not the function -- and refusing that refused a legitimate suite.
    Anything unresolvable yields ``""``.
    """
    current: ast.expr = node
    for _ in range(_RECEIVER_RESOLUTION_DEPTH):
        if isinstance(current, (ast.Attribute, ast.Subscript)):
            current = current.value
        elif isinstance(current, ast.BinOp):
            current = current.left
        elif isinstance(current, ast.Call):
            # Two shapes root a path in a fixture, and both must resolve:
            # `tmp_path.joinpath("a")` roots through the *callable*, while
            # `make_file(tmp_path)` roots through the *argument*. Trying the
            # callable first keeps the original behaviour intact and only
            # falls back to the argument when the callable leads nowhere.
            via_func = _receiver_root(current.func)
            if via_func in SANDBOXED_RECEIVERS:
                return via_func
            if current.args:
                via_arg = _receiver_root(current.args[0])
                if via_arg in SANDBOXED_RECEIVERS:
                    return via_arg
            current = current.func
        else:
            break
    return current.id if isinstance(current, ast.Name) else ""


#: Bounds the walk above; chains deeper than this in a test suite are not a
#: shape worth resolving, and an unbounded loop on hostile input is not either.
_RECEIVER_RESOLUTION_DEPTH = 16


def _is_opaque_getattr(node: ast.Call, aliases: Mapping[str, str] | None = None) -> bool:
    """True when ``getattr`` is used to reach an attribute indirectly.

    Blanket-flagging every ``getattr`` was noise: ``getattr(obj, "name",
    default)`` is ordinary Python. But a literal name is not evidence of
    innocence either -- ``getattr(os, "system")("ls")`` is exactly the
    indirection this tripwire exists to catch. The distinction that actually
    matters is the *receiver*: reaching into a dangerous module by name is
    refused whatever the form, and a plain three-argument lookup on an
    ordinary object is not.
    """
    if _dotted_name(node.func, aliases) != "getattr" or len(node.args) < 2:
        return False
    receiver = _receiver_root(node.args[0])
    resolved = aliases.get(receiver, receiver) if aliases else receiver
    if resolved.split(".")[0] in DANGEROUS_GETATTR_RECEIVERS:
        return True
    name_arg = node.args[1]
    return not (isinstance(name_arg, ast.Constant) and isinstance(name_arg.value, str))


#: Modules whose attributes are dangerous however they are reached. Resolving
#: ``getattr(os, "system")`` requires naming them, since the AST cannot follow
#: the resulting callable to its call site.
DANGEROUS_GETATTR_RECEIVERS: frozenset[str] = frozenset(
    {"os", "subprocess", "shutil", "socket", "ctypes", "pickle", "marshal", "builtins"}
)


def _sandboxed_aliases(tree: ast.AST) -> frozenset[str]:
    """Local names that hold a path derived from an isolated pytest fixture.

    Suites almost never call ``tmp_path.write_text`` directly; they write
    ``p = tmp_path / "d.json"`` first. Resolving only the direct chain
    therefore still refused the idiomatic form, so aliases are propagated to a
    fixed point (``p = tmp_path / "a"``; ``q = p.parent``; both are confined).
    Only names whose value is *rooted* in a fixture are added -- an assignment
    from anything else, including a later rebind, is not.
    """
    aliases: set[str] = set(SANDBOXED_RECEIVERS)
    # A fixed point is needed because assignments may appear in any order.
    for _ in range(_ALIAS_RESOLUTION_PASSES):
        grew = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.NamedExpr)):
                continue
            value = node.value
            if value is None:
                continue
            if _receiver_root(value) not in aliases:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in aliases:
                    aliases.add(target.id)
                    grew = True
        if not grew:
            break
    return frozenset(aliases)


#: Alias propagation is a fixed point over assignment order; suites are small
#: and this bounds a pathological chain rather than looping unbounded.
_ALIAS_RESOLUTION_PASSES = 8


def scan_source(source: str) -> ScanVerdict:
    """Classify untrusted source into refusals and warnings.

    This is a tripwire, not a perimeter: string concatenation and ``getattr``
    indirection defeat any static scan. Real containment comes from the
    container/rlimit backend in :func:`run_sandboxed`. Its job is to be right
    in *both* directions -- a scanner that refuses good suites is as broken as
    one that admits bad ones.
    """
    refuse: list[str] = []
    warn: list[str] = []

    scanned = _strip_comments_and_strings(source)
    for fragment, reason in DESTRUCTIVE_PATTERNS:
        if fragment in scanned:
            refuse.append(reason)

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return ScanVerdict(refuse=_dedupe(refuse), warn=_dedupe(warn))

    confined = _sandboxed_aliases(tree)
    aliases = _import_aliases(tree)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        dotted = _dotted_name(node.func, aliases)
        if dotted in DANGEROUS_CALL_PATHS:
            refuse.append(f"calls dangerous function '{dotted}'")
            continue
        if _is_writable_open(node, dotted):
            refuse.append("opens a file for writing via open()")
            continue
        if _is_opaque_getattr(node, aliases):
            refuse.append("dynamic attribute lookup via getattr")
            continue
        if isinstance(node.func, ast.Attribute) and node.func.attr in DANGEROUS_METHOD_NAMES:
            receiver = _receiver_root(node.func.value)
            if receiver in confined:
                warn.append(
                    f"writes through pytest fixture '{receiver}' via '{node.func.attr}' (allowed)"
                )
            else:
                refuse.append(f"calls filesystem-mutating method '{node.func.attr}'")

    return ScanVerdict(refuse=_dedupe(refuse), warn=_dedupe(warn))


#: Modes that make ``open`` a write. ``open(p)`` and ``open(p, "r")`` are
#: ordinary reads and stay allowed; anything that can create, truncate or
#: append is a filesystem mutation the tripwire must see.
_WRITE_MODE_CHARS: frozenset[str] = frozenset({"w", "a", "x", "+"})


def _is_writable_open(node: ast.Call, dotted: str) -> bool:
    """True for ``open(path, "w")`` and friends, including ``Path.open``.

    ``open(...).write(...)`` reached the host filesystem untouched: the AST
    pass only looked at the *call target*, and ``open`` was not in the dotted
    table, while the ``write`` method call had an unresolvable receiver that
    is not in :data:`DANGEROUS_METHOD_NAMES`.
    """
    if dotted != "open" and not (isinstance(node.func, ast.Attribute) and node.func.attr == "open"):
        return False
    mode: str | None = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        value = node.args[1].value
        mode = value if isinstance(value, str) else None
    for keyword in node.keywords:
        if keyword.arg == "mode" and isinstance(keyword.value, ast.Constant):
            value = keyword.value.value
            mode = value if isinstance(value, str) else mode
    if mode is None:
        return False
    return bool(set(mode) & _WRITE_MODE_CHARS)


def _dedupe(reasons: list[str]) -> tuple[str, ...]:
    """Order-preserving de-duplication: one reason per distinct problem."""
    seen: set[str] = set()
    unique: list[str] = []
    for reason in reasons:
        if reason not in seen:
            seen.add(reason)
            unique.append(reason)
    return tuple(unique)


def scan_for_dangerous_calls(source: str) -> list[str]:
    """Reasons the source must not be executed. Empty list means "looks ok".

    Thin wrapper over :func:`scan_source` kept as the stable public API; only
    the refusal set is returned, because only refusals stop execution.
    """
    verdict = scan_source(source)
    for note in verdict.warn:
        logger.debug("sandbox scan note: %s", note)
    return list(verdict.refuse)


class SandboxUnavailableError(RuntimeError):
    """No isolation backend is available and the policy is fail-closed.

    Raised instead of silently executing LLM-authored code unconfined. The
    previous behaviour degraded to a bare ``subprocess.run`` on any platform
    without ``fork`` (i.e. every Windows host) while the documentation still
    promised resource limits -- the one fail-*open* path in an otherwise
    fail-closed system. See ADR-008.
    """


#: Accepted values for ``execution.sandbox_backend``.
SANDBOX_BACKENDS: tuple[str, ...] = ("auto", "rlimit", "docker", "podman", "wsl", "none")

#: Effective backends ``resolve_backend`` may return.
EFFECTIVE_BACKENDS: tuple[str, ...] = ("rlimit", "docker", "podman", "wsl", "none")

#: Container runtimes share one locked-down ``run`` contract (read-only,
#: no network, cpu/memory/pids capped). Either binary satisfies it.
CONTAINER_RUNTIMES: tuple[str, ...] = ("docker", "podman")

Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def posix_rlimits_available() -> bool:
    """True when ``preexec_fn`` + ``resource`` rlimits can be installed."""
    return hasattr(os, "fork")


def docker_available(runner: Runner | None = None, *, timeout: float = 5.0) -> bool:
    """True when a responsive Docker daemon can be reached.

    ``docker version`` is used rather than ``docker --version`` because the
    latter succeeds even when the daemon is down, which would let us claim
    container isolation we cannot actually provide.
    """
    return _docker_probe("docker", runner, timeout=timeout)


def podman_available(runner: Runner | None = None, *, timeout: float = 5.0) -> bool:
    """True when a running Podman machine (and the ``podman`` binary) exists.

    Podman is a drop-in for Docker for the ``run`` call, but its ``version``
    command exits 0 even when **no machine is running** -- the same fail-open
    trap ADR-008 closed for ``docker --version``. So we probe ``podman info``
    (which reaches the machine/VM) and treat anything that does not confirm a
    live runtime as "not available" rather than "isolation ready".
    """
    return _docker_probe("podman", runner, info=True, timeout=timeout)


def _docker_probe(
    binary: str, runner: Runner | None, *, info: bool = False, timeout: float = 5.0
) -> bool:
    """Probe a Docker-compatible runtime for a *responsive* daemon/machine.

    For Docker the canonical probe is ``<bin> version`` (server component).
    For Podman we use ``<bin> info``, because ``podman version`` reports the
    client version and exits 0 even with no machine booted. Either probe must
    return a clean exit code, or we report no isolation. The timeout is short
    on purpose: ``--doctor`` probes both runtimes, and a 15s hang each made a
    host with neither installed sit silent for half a minute.
    """
    run = runner or subprocess.run
    argv = [binary, "info"] if info else [binary, "version", "--format", "{{.Server.Version}}"]
    try:
        proc = run(  # nosec B603 B607 - fixed argv, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def wsl_available(runner: Runner | None = None, *, timeout: float = 15.0) -> bool:
    """True when ``wsl.exe`` can actually execute a command in a Linux distro.

    The probe deliberately *runs* something (``/bin/sh -c 'exit 0'``) rather
    than asking ``wsl -l``: on a Windows host with the WSL feature enabled but
    **no distribution installed**, ``wsl -l`` and ``wsl --status`` still exit
    0 while any real command exits 127. Trusting those would claim isolation
    we cannot deliver -- the same fail-open trap ADR-008 closed for
    ``docker --version``. Measured on the reference Windows host: ``wsl -l -q``
    exit 0, ``wsl -e /bin/sh -c 'exit 0'`` exit 127, so this returns False.

    The timeout is longer than the container probes because a cold WSL VM has
    to boot before the first command runs.
    """
    run = runner or subprocess.run
    try:
        proc = run(  # nosec B603 B607 - fixed argv, no shell
            ["wsl.exe", "-e", "/bin/sh", "-c", "exit 0"],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def to_wsl_path(value: str) -> str:
    """Rewrite a Windows path into its ``/mnt/<drive>`` WSL form.

    ``C:\\xampp\\htdocs\\looper`` becomes ``/mnt/c/xampp/htdocs/looper``.
    Anything that is not a drive-letter absolute path is returned unchanged:
    pytest node ids, flags, and paths already in POSIX form must survive.
    """
    match = re.match(r"^([A-Za-z]):[\\/](.*)$", value)
    if match is None:
        return value
    drive, tail = match.group(1).lower(), match.group(2).replace("\\", "/")
    return f"/mnt/{drive}" if not tail else f"/mnt/{drive}/{tail}"


def wsl_argv(
    argv: list[str],
    *,
    cwd: str,
    cpu_seconds: int,
    rss_bytes: int,
) -> list[str]:
    """Wrap ``argv`` so it runs inside WSL under POSIX rlimits.

    This is the answer to the single biggest adoption blocker: on a Windows
    host without Docker Desktop, ``resolve_backend`` had nothing to offer and
    every build refused to run its tests (``--doctor`` exit 5). WSL2 is a real
    Linux kernel, so ``ulimit`` gives the same RLIMIT_CPU / RLIMIT_AS /
    RLIMIT_NPROC / RLIMIT_FSIZE guarantees the ``rlimit`` backend provides on
    native POSIX.

    **It is weaker than a container and deliberately not treated as equal:**
    WSL shares the host network stack and can reach the Windows filesystem
    through ``/mnt``, so it bounds *resource* blast radius, not *network* or
    *filesystem* blast radius. ``auto`` therefore prefers Docker/Podman first
    and only falls back here, and the static tripwire scan still runs before
    anything is executed.
    """
    limits = (
        f"ulimit -t {max(1, cpu_seconds)}; "
        f"ulimit -v {max(65536, rss_bytes // 1024)}; "
        "ulimit -u 64 2>/dev/null || true; "
        "ulimit -f 65536 2>/dev/null || true; "
    )
    inner = " ".join(shlex.quote(to_wsl_path(arg)) for arg in argv[1:])
    command = f"{limits}exec python3 {inner}"
    return ["wsl.exe", "--cd", cwd, "-e", "/bin/sh", "-c", command]


def container_runtime_available(*, runner: Runner | None = None) -> str | None:
    """First available Docker-compatible runtime, or ``None``.

    Prefers Docker over Podman: the existing agents' ``sandbox_image`` default
    and ``docker_argv`` wiring are Docker-shaped, and Podman's rootless setup
    is a strict superset of what Docker needs. Returning the binary name lets
    ``resolve_backend`` feed it straight into the shared ``docker_argv``.
    """
    for runtime in CONTAINER_RUNTIMES:
        if runtime == "docker" and docker_available(runner):
            return "docker"
        if runtime == "podman" and podman_available(runner):
            return "podman"
    return None


def resolve_backend(
    requested: str,
    *,
    fail_closed: bool = True,
    runner: Runner | None = None,
) -> str:
    """Pick the effective isolation backend, or refuse.

    ``auto`` prefers Docker (equivalent isolation on every OS), then POSIX
    rlimits. When nothing is available the decision is the caller's policy:
    ``fail_closed=True`` raises :class:`SandboxUnavailableError` so untrusted
    code is never run unconfined, ``False`` degrades to ``none`` with a loud
    warning.
    """
    if requested not in SANDBOX_BACKENDS:
        raise ValueError(f"unknown sandbox backend {requested!r}; expected {SANDBOX_BACKENDS}")

    if requested == "none":
        logger.warning(
            "sandbox_backend='none': LLM-authored tests will run unconfined on this host"
        )
        return "none"

    if requested in ("docker", "podman"):
        if requested == "docker" and docker_available(runner):
            return "docker"
        if requested == "podman" and podman_available(runner):
            return "podman"
        message = (
            "docker backend requested but no Docker daemon responded"
            if requested == "docker"
            else "podman backend requested but no running Podman machine was found"
        )
        return _unavailable(message, fail_closed)

    if requested == "rlimit":
        if posix_rlimits_available():
            return "rlimit"
        return _unavailable(
            "rlimit backend requested but this platform has no fork/rlimits", fail_closed
        )

    if requested == "wsl":
        if wsl_available(runner):
            return "wsl"
        return _unavailable(
            "wsl backend requested but wsl.exe could not run a command "
            "(is a distribution installed? try `wsl --install`)",
            fail_closed,
        )

    # auto
    runtime = container_runtime_available(runner=runner)
    if runtime is not None:
        return runtime
    if posix_rlimits_available():
        return "rlimit"
    # Last resort before refusing: on Windows WSL2 is a real Linux kernel, so
    # ulimit gives genuine resource containment where nothing else could. It
    # is ranked below containers because it shares the host network and can
    # see the Windows filesystem via /mnt.
    if wsl_available(runner):
        return "wsl"
    return _unavailable(
        "no sandbox backend available (no Docker/Podman daemon, no POSIX rlimits, no WSL distro)",
        fail_closed,
    )


def _unavailable(message: str, fail_closed: bool) -> str:
    if fail_closed:
        raise SandboxUnavailableError(message)
    logger.warning("%s; running WITHOUT isolation because sandbox_fail_closed is false", message)
    return "none"


def to_container_path(value: str, cwd: str) -> str:
    """Rewrite a host path that lives under ``cwd`` into its ``/work`` form.

    ``argv`` carries the absolute host path of the tests directory. Inside the
    image only ``/work`` exists, so passing ``C:\\ws\\tests`` (or
    ``/home/u/ws/tests``) made pytest exit non-zero every single time -- the
    container backend, the only real isolation on Windows/macOS and the whole
    of ADR-008, could never once succeed. Values outside ``cwd`` are returned
    unchanged; the caller is responsible for refusing those.
    """
    host_root = str(Path(cwd))
    try:
        candidate = Path(value)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return value
    if not candidate.is_absolute():
        return value
    try:
        relative = candidate.relative_to(host_root)
    except ValueError:
        return value
    tail = relative.as_posix()
    return "/work" if tail in ("", ".") else f"/work/{tail}"


def docker_argv(
    argv: list[str],
    *,
    cwd: str,
    image: str,
    network: str,
    cpu_seconds: int,
    rss_bytes: int,
    runtime: str = "docker",
    cpu_shares: float = 1.0,
) -> list[str]:
    """Wrap ``argv`` so it runs inside a throwaway, network-isolated container.

    The host interpreter path in ``argv[0]`` is meaningless inside the image,
    so it is replaced with the container's ``python``. Every remaining
    argument that points inside the workspace is rewritten to its ``/work``
    equivalent, because the workspace is bind-mounted there and the host path
    does not exist in the image. The workspace is the only thing mounted,
    which also contains the filesystem blast radius. ``runtime`` is the
    resolved binary (``docker`` or ``podman``); both share this exact ``run``
    contract (read-only, no network, capped cpu/mem/pids).

    ``--cpus`` is a *scheduler share*, not a CPU-time budget: it throttles how
    fast a runaway loop burns, but never stops it. It is therefore its own
    knob (``sandbox_cpu_shares``) rather than being derived from
    ``sandbox_cpu_seconds`` -- the old ``cpu_seconds // 60`` formula handed a
    600-second budget **ten whole CPUs**, quietly making the sandbox more
    powerful the longer it was allowed to run. ``sandbox_cpu_seconds`` is
    passed as ``--ulimit cpu=``, which is RLIMIT_CPU inside the container and
    does kill the process -- giving the container backend the same guarantee
    the rlimit backend already had.
    """
    inner = [to_container_path(arg, cwd) for arg in argv[1:]]
    return [
        runtime,
        "run",
        "--rm",
        f"--network={network}",
        f"--cpus={_format_cpu_shares(cpu_shares)}",
        f"--ulimit=cpu={max(1, cpu_seconds)}",
        f"--memory={max(64_000_000, rss_bytes)}b",
        "--pids-limit=256",
        "--read-only",
        "--tmpfs=/tmp:rw,size=64m",  # nosec B108 - container-internal tmpfs, not a host path
        "--security-opt=no-new-privileges",
        "-v",
        f"{cwd}:/work",
        "-w",
        "/work",
        image,
        "python",
        *inner,
    ]


def _format_cpu_shares(cpu_shares: float) -> str:
    """Render ``--cpus`` without a pointless ``.0`` tail.

    Docker accepts both ``1`` and ``1.0``, but the integer form is what
    operators write in config and what appears in the docs, so keeping the
    argv readable keeps the two comparable.
    """
    clamped = max(0.1, cpu_shares)
    return str(int(clamped)) if clamped == int(clamped) else str(clamped)


def run_sandboxed(
    argv: list[str],
    *,
    cwd: str,
    timeout: int,
    cpu_seconds: int,
    wall_seconds: int,
    rss_bytes: int,
    backend: str = "auto",
    image: str = "python:3.11-slim",
    network: str = "none",
    fail_closed: bool = True,
    runner: Runner | None = None,
    cpu_shares: float = 1.0,
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` (fixed, never shell) under the strongest isolation available.

    * ``docker`` -- throwaway read-only container, no network, cpu/memory/pids
      capped. Identical guarantees on Linux, macOS and Windows (needs a Docker
      daemon / Desktop).
    * ``podman`` -- same throwaway container contract via the ``podman`` binary
      (needs a running Podman machine on Windows/macOS). The ``run`` flags are
      shared with Docker; only the binary differs.
    * ``rlimit`` -- POSIX ``preexec_fn`` installing RLIMIT_CPU / RLIMIT_AS.
    * ``none``   -- no isolation; only reachable when the caller explicitly
      opted out or set ``fail_closed=False``.

    Raises :class:`SandboxUnavailableError` when isolation was required but
    could not be provided.
    """
    run = runner or subprocess.run
    effective = resolve_backend(backend, fail_closed=fail_closed, runner=runner)

    if effective in ("docker", "podman"):
        return run(  # nosec B603 - fixed argv, no shell
            docker_argv(
                argv,
                cwd=cwd,
                image=image,
                network=network,
                cpu_seconds=cpu_seconds,
                rss_bytes=rss_bytes,
                runtime=effective,
                cpu_shares=cpu_shares,
            ),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    if effective == "wsl":
        return run(  # nosec B603 - fixed argv, no shell
            wsl_argv(argv, cwd=cwd, cpu_seconds=cpu_seconds, rss_bytes=rss_bytes),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    preexec = (
        _posix_rlimit_fn(cpu_seconds, wall_seconds, rss_bytes) if effective == "rlimit" else None
    )
    return run(  # nosec B603 - fixed argv, no shell
        argv,
        capture_output=True,
        text=True,
        cwd=cwd,
        timeout=timeout,
        check=False,
        preexec_fn=preexec,
    )


def _posix_rlimit_fn(cpu_seconds: int, wall_seconds: int, rss_bytes: int) -> "Callable[[], None]":
    """Build a ``preexec_fn`` that installs CPU/AS rlimits (POSIX assumes fork).

    Three bugs lived here and were invisible because the function carried a
    ``# pragma: no cover``:

    * ``setrlimit`` was passed a bare int where the API requires a
      ``(soft, hard)`` tuple -- a guaranteed ``TypeError`` inside the child;
    * the error-handling helper ``_set`` was defined but never called;
    * a final ``RLIMIT_CPU`` write with ``wall_seconds`` silently overwrote
      the real CPU cap. RLIMIT_CPU never bounded wall clock anyway -- the
      subprocess ``timeout`` does that, so ``wall_seconds`` is not an rlimit.
    """
    import resource

    def _preexec() -> None:
        def _set(limit: int, value: int) -> None:
            try:
                resource.setrlimit(limit, (value, value))  # type: ignore[attr-defined]
            except (ValueError, OSError) as exc:
                logger.warning("Could not set rlimit %s: %s", limit, exc)

        _set(resource.RLIMIT_CPU, max(1, cpu_seconds))  # type: ignore[attr-defined]
        _set(resource.RLIMIT_AS, max(1, rss_bytes))  # type: ignore[attr-defined]
        # Fork bombs and runaway file writes are the other two ways generated
        # code wedges a host; both are cheap to cap here.
        nproc = getattr(resource, "RLIMIT_NPROC", None)
        if nproc is not None:
            _set(nproc, 64)
        fsize = getattr(resource, "RLIMIT_FSIZE", None)
        if fsize is not None:
            _set(fsize, 64 * 1024 * 1024)

    return _preexec
