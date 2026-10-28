#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#
"""Run a command in Docker, under Landlock, or natively, as selected by --runner.

usage: sandbox.py [--runner docker:IMAGE|landlock|native] [--root DIR] [--workdir DIR]
                  [--hide DIR]... [--readable PATH | --writable PATH]...
                  [--env NAME=value | NAME=value]... [--tty] [--inspect] -- [@]command...

The command sees the root directory read-only, except for the directories hidden with --hide
and the working directory, which are empty unless the paths mounted with --readable or
--writable are inside them. Relative paths are taken from, and must stay inside, the root.

- docker:IMAGE runs the step in a container, hiding paths under tmpfs mounts.
- landlock gives the same view where Docker is unavailable (e.g. inside an unprivileged
  container): a Landlock filesystem sandbox, which needs no privileges and confines every
  descendant process; any still running when the step exits are terminated, as in Docker.
  The rest of the filesystem is limited to system and toolchain
  directories, HOME and TMPDIR are fresh per step, and network access is not restricted.
  Landlock does not cover metadata, so the names, permissions, timestamps and extended
  attributes of paths outside the mounts can still be read or changed.
- native runs the step unconfined.

The command is echoed to fd 3 (or stderr) unless its name is prefixed with @.

This file runs standalone (`uv run --script`) before any project environment exists, so it
must only use the standard library and support the Python version declared above.
"""

import sys

# Run as a script, this file's directory heads sys.path, where e.g. ideas/ast.py would
# shadow the standard library
if __name__ == "__main__" and not sys.flags.safe_path:
    sys.path.pop(0)

import ctypes
import errno
import fcntl
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from typing import NoReturn

PROG = "sandbox.py"
USAGE = (
    f"usage: {PROG} [--runner docker:IMAGE|landlock|native] [--root DIR] [options]"
    " [NAME=value ...] -- command..."
)
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
UNSAFE_RELATIVE = re.compile(r"(^|/)\.\.?(/|$)")


def trace_fd() -> int:
    """make passes 3>&2 so command traces stay visible when a step's stderr goes to a log.

    Without it fd 3 is free, and Python or this script may open something else there (e.g.
    /dev/urandom or the Landlock ruleset), so this must run before anything is opened.
    """
    try:
        mode = os.fstat(3).st_mode
        flags = fcntl.fcntl(3, fcntl.F_GETFL)
    except OSError:
        return 2
    output = (
        stat.S_ISFIFO(mode) or stat.S_ISREG(mode) or stat.S_ISCHR(mode) or stat.S_ISSOCK(mode)
    )
    return 3 if output and flags & os.O_ACCMODE in (os.O_WRONLY, os.O_RDWR) else 2


ECHO_FD = trace_fd()


def die(message: str) -> NoReturn:
    print(f"{PROG}: {message}", file=sys.stderr)
    sys.exit(2)


def usage() -> NoReturn:
    print(USAGE, file=sys.stderr)
    sys.exit(2)


@dataclass
class Mount:
    path: str
    writable: bool


@dataclass
class Options:
    runner: str = "native"
    image: str = ""
    root: str = ""
    workdir: str = ""
    hides: list[str] = field(default_factory=list)
    mounts: list[Mount] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    inspect: bool = False
    tty: bool = False
    echo: bool = True
    command: list[str] = field(default_factory=list)


def add_env(opts: Options, spec: str) -> None:
    if "=" not in spec:
        die(f"environment assignment must contain '=': {spec}")
    name = spec.split("=", 1)[0]
    if not ENV_NAME.fullmatch(name):
        die(f"invalid environment variable name: {name}")
    opts.env.append(spec)


def parse_args(argv: list[str]) -> Options:
    opts = Options()
    args = iter(argv)
    for arg in args:
        if arg == "--":
            opts.command = list(args)
            break
        if arg in (
            "--runner",
            "--root",
            "--workdir",
            "--hide",
            "--readable",
            "--writable",
            "--env",
        ):
            value = next(args, None)
            if value is None:
                usage()
            if arg == "--runner":
                if value in ("native", "landlock"):
                    opts.runner = value
                elif value.startswith("docker:") and value != "docker:":
                    opts.runner, opts.image = "docker", value.removeprefix("docker:")
                else:
                    die(f"unknown runner: {value} (expected docker:IMAGE, landlock or native)")
            elif arg == "--root":
                opts.root = value
            elif arg == "--workdir":
                opts.workdir = value
            elif arg == "--hide":
                opts.hides.append(value)
            elif arg == "--env":
                add_env(opts, value)
            else:
                opts.mounts.append(Mount(value, arg == "--writable"))
        elif arg == "--inspect":
            opts.inspect = opts.tty = True
        elif arg == "--tty":
            opts.tty = True
        elif "=" in arg:
            add_env(opts, arg)
        else:
            usage()
    if not opts.command:
        usage()
    if opts.command[0].startswith("@"):
        opts.command[0] = opts.command[0][1:]
        if not opts.command[0]:
            die("command name must not be empty")
        opts.echo = opts.inspect
    return opts


def resolve_dir(path: str, what: str) -> str:
    if not os.path.isdir(path):
        die(f"{what} does not exist: {path}")
    return os.path.realpath(path)


def resolve_path(root: str, path: str, what: str) -> str:
    """Resolves a path, with a relative one taken from and confined to root."""
    if not path or ":" in path:
        die(f"{what} path must not be empty or contain ':': {path}")
    if os.path.isabs(path):
        return os.path.realpath(path)
    if UNSAFE_RELATIVE.search(path):
        die(f"relative {what} path contains an unsafe path: {path}")
    real = os.path.realpath(os.path.join(root, path))
    if not real.startswith(root + "/"):
        die(f"relative {what} path must stay inside the root: {path}")
    return real


def resolve_mounts(root: str, mounts: list[Mount]) -> list[Mount]:
    resolved: dict[str, bool] = {}
    for mount in mounts:
        real = resolve_path(root, mount.path, "mount")
        if not os.path.isdir(real) and not os.path.isfile(real):
            die(f"mount path does not exist: {mount.path}")
        if resolved.setdefault(real, mount.writable) != mount.writable:
            die(f"mount directory has conflicting modes: {real}")
    return [Mount(path, writable) for path, writable in resolved.items()]


def resolve_hides(root: str, hides: list[str]) -> list[str]:
    """Resolves the directories to hide, skipping missing ones as there is nothing to hide."""
    resolved: dict[str, None] = {}
    for path in hides:
        real = resolve_path(root, path, "hidden")
        if os.path.exists(real) and not os.path.isdir(real):
            die(f"hidden path must be a directory: {path}")
        if os.path.isdir(real):
            resolved[real] = None
    return list(resolved)


def echo(opts: Options, label: str, command: list[str], details: list[str]) -> None:
    """Prints the command, with the details (env or docker args) dimmed, to fd 3 or stderr."""
    if not opts.echo:
        return
    fd = ECHO_FD
    if os.isatty(fd) and not os.environ.get("NO_COLOR"):
        line = f"\033[1;36m[{label}]\033[0m "
        if details:
            line += f"\033[90m{shlex.join(details)}\033[0m "
    else:
        line = f"[{label}] " + "".join(f"{shlex.quote(d)} " for d in details)
    os.write(fd, f"{line}{shlex.join(command)}\n".encode())


def exec_command(command: list[str], env: dict[str, str]) -> NoReturn:
    try:
        os.execvpe(command[0], command, env)
    except OSError as e:
        die_exec(command[0], e)


def die_exec(name: str, error: OSError) -> NoReturn:
    print(f"{PROG}: {name}: {error.strerror}", file=sys.stderr)
    sys.exit(127 if isinstance(error, FileNotFoundError) else 126)


def with_env(specs: list[str]) -> dict[str, str]:
    env = dict(os.environ)
    for spec in specs:
        name, value = spec.split("=", 1)
        env[name] = value
    return env


def run_native(opts: Options) -> NoReturn:
    if opts.workdir:
        os.chdir(resolve_dir(opts.workdir, "working directory"))
    echo(opts, "NATIVE", opts.command, ["env", *opts.env])
    exec_command(opts.command, with_env(opts.env))


# Landlock

SYS_CREATE_RULESET, SYS_ADD_RULE, SYS_RESTRICT_SELF = 444, 445, 446
CREATE_RULESET_VERSION = 1 << 0
RULE_PATH_BENEATH = 1
PR_SET_PDEATHSIG, PR_SET_CHILD_SUBREAPER, PR_SET_NO_NEW_PRIVS = 1, 36, 38

EXECUTE, WRITE_FILE, READ_FILE, READ_DIR = 1 << 0, 1 << 1, 1 << 2, 1 << 3
TRUNCATE, IOCTL_DEV = 1 << 14, 1 << 15
READ = EXECUTE | READ_FILE | READ_DIR
FILE_RIGHTS = EXECUTE | WRITE_FILE | READ_FILE | TRUNCATE | IOCTL_DEV
SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/etc", "/opt")


class RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class PathBeneathAttr(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


@dataclass
class LandlockPolicy:
    """Paths granted read-only (False) or writable (True), and paths hidden within them."""

    grants: dict[str, bool] = field(default_factory=dict)
    hides: set[str] = field(default_factory=set)

    def grant(self, path: str, writable: bool = False) -> None:
        path = os.path.realpath(path)
        self.grants[path] = self.grants.get(path, False) or writable

    def hide(self, path: str) -> None:
        self.hides.add(os.path.realpath(path))

    def rules(self) -> dict[str, bool]:
        """Splits each grant around the hidden paths below it, since Landlock only adds
        access; a grant on a path inside a hidden one re-exposes it."""
        rules: dict[str, bool] = {}

        def expand(path: str, writable: bool) -> None:
            if path in self.hides:
                return
            prefix = path.rstrip("/") + "/"
            if os.path.isdir(path) and any(h.startswith(prefix) for h in self.hides):
                with os.scandir(path) as entries:
                    for entry in entries:
                        # A symlink needs no rule; following it would grant its target
                        if not entry.is_symlink():
                            expand(entry.path, writable)
                return
            rules[path] = rules.get(path, False) or writable

        for path, writable in self.grants.items():
            if os.path.exists(path):
                expand(path, writable)
        return rules


def landlock_policy(
    root: str,
    workdir: str,
    hides: list[str],
    mounts: list[Mount],
    scratch: str,
    environ: dict[str, str],
) -> tuple[LandlockPolicy, dict[str, str]]:
    """Mirrors the Docker container's view, returning the policy and the step's env."""
    home = os.path.realpath(environ.get("HOME") or die("HOME must be set"))
    cargo_home = environ.get("CARGO_HOME") or f"{home}/.cargo"
    rustup_home = environ.get("RUSTUP_HOME") or f"{home}/.rustup"
    uv_data_dir = (environ.get("XDG_DATA_HOME") or f"{home}/.local/share") + "/uv"
    uv_python_dir = environ.get("UV_PYTHON_INSTALL_DIR") or f"{uv_data_dir}/python"

    policy = LandlockPolicy()
    policy.hide(home)
    for path in hides:
        policy.hide(path)
    if workdir != root and not any(m.path == workdir for m in mounts):
        policy.hide(workdir)
    for path in (*SYSTEM_DIRS, "/proc", "/sys", root, rustup_home, cargo_home):
        policy.grant(path)
    policy.grant(uv_python_dir)
    # e.g. systemd-resolved's stub, which /etc/resolv.conf links to under /run
    policy.grant("/etc/resolv.conf")
    policy.grant("/dev", writable=True)
    policy.grant(f"{cargo_home}/registry", writable=True)
    policy.grant(f"{cargo_home}/git", writable=True)

    # HOME is hidden, so a PATH entry inside it re-exposes only these: a tool's prefix lib and
    # libexec (e.g. bear's helpers), and the install dir of a symlinked tool (e.g. codex)
    for entry in environ.get("PATH", "").split(":"):
        if not entry.startswith("/") or not os.path.isdir(entry):
            continue
        path_dir = os.path.realpath(entry)
        if path_dir == "/":
            continue
        policy.grant(path_dir)
        if path_dir in ("/usr", "/bin", "/sbin") or path_dir.startswith("/usr/"):
            continue
        if path_dir.endswith("/bin"):
            for lib in ("lib", "lib64", "libexec"):
                policy.grant(f"{path_dir.removesuffix('/bin')}/{lib}")
        try:
            with os.scandir(path_dir) as tools:
                links = [t.path for t in tools if t.is_symlink() and not t.name.startswith(".")]
        except OSError:
            continue
        for link in links:
            if os.path.exists(link):
                policy.grant(os.path.dirname(os.path.realpath(link)))

    for mount in mounts:
        policy.grant(mount.path, mount.writable)
    # Landlock only adds access, so a writable mount would leave everything below it writable
    for inner in mounts:
        for outer in mounts:
            if (
                outer.writable
                and not inner.writable
                and inner.path.startswith(outer.path + "/")
            ):
                die(
                    f"read-only mount {inner.path} inside writable mount {outer.path} cannot"
                    " be enforced with Landlock"
                )
    policy.grant(scratch, writable=True)

    # Pin toolchain locations before HOME moves, and keep uv from writing to the venv or
    # the host's uv cache, as in Docker
    env = {
        "HOME": f"{scratch}/home",
        "TMPDIR": f"{scratch}/tmp",
        "CARGO_HOME": cargo_home,
        "RUSTUP_HOME": rustup_home,
        "UV_PYTHON_INSTALL_DIR": uv_python_dir,
        "UV_CACHE_DIR": f"{scratch}/uv-cache",
        "UV_NO_SYNC": "1",
    }
    return policy, env


class Landlock:
    def __init__(self) -> None:
        self.libc = ctypes.CDLL(None, use_errno=True)
        abi = self.libc.syscall(SYS_CREATE_RULESET, None, 0, CREATE_RULESET_VERSION)
        if abi < 0:
            err = ctypes.get_errno()
            reason = {
                errno.ENOSYS: "the kernel was built without it",
                errno.EOPNOTSUPP: "it is disabled at boot (see /sys/kernel/security/lsm)",
                errno.EPERM: "a seccomp filter (e.g. an old Docker profile) blocks it",
            }.get(err, os.strerror(err))
            die(f"Landlock is unavailable: {reason}")
        if abi < 3:
            die(
                f"Landlock ABI {abi} cannot stop truncate() from erasing files outside the"
                " sandbox; Linux 6.2+ is required (or use a docker:IMAGE or native runner)"
            )
        # ABI 3 has 15 filesystem rights (up to TRUNCATE) and ABI 5 adds IOCTL_DEV
        self.access = (1 << (15 if abi < 5 else 16)) - 1

    def ruleset(self, rules: dict[str, bool]) -> int:
        """Creates a ruleset fd (close-on-exec) that grants exactly the rules."""
        attr = RulesetAttr(self.access)
        ruleset = self.libc.syscall(
            SYS_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0
        )
        if ruleset < 0:
            die(f"landlock_create_ruleset: {os.strerror(ctypes.get_errno())}")
        for path, writable in rules.items():
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except FileNotFoundError:
                continue
            allowed = self.access if writable else READ
            if not os.path.isdir(path):
                allowed &= FILE_RIGHTS
            rule = PathBeneathAttr(allowed & self.access, fd)
            if self.libc.syscall(
                SYS_ADD_RULE, ruleset, RULE_PATH_BENEATH, ctypes.byref(rule), 0
            ):
                die(f"landlock_add_rule {path}: {os.strerror(ctypes.get_errno())}")
            os.close(fd)
        return ruleset

    def restrict_self(self, ruleset: int, parent: int) -> None:
        """Confines the calling process; runs in the child between fork and exec."""
        # Die with sandbox.py. Forked children do not inherit this, so like a killed `docker run`
        # client, a SIGKILL leaves them running; otherwise reap_descendants() stops them.
        if (
            self.libc.prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
            or os.getppid() != parent
            or self.libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
            or self.libc.syscall(SYS_RESTRICT_SELF, ruleset, 0)
        ):
            message = f"{PROG}: cannot confine the step: {os.strerror(ctypes.get_errno())}\n"
            os.write(2, message.encode())
            os._exit(2)


def remove_tree(path: str) -> None:
    """Removes a scratch tree even where the step dropped its own write permissions."""
    # Top-down, so each directory is accessible again before os.walk lists it
    os.chmod(path, stat.S_IRWXU)
    for root, dirs, _ in os.walk(path):
        for name in dirs:
            target = os.path.join(root, name)
            if not os.path.islink(target):
                os.chmod(target, os.stat(target).st_mode | stat.S_IRWXU)
    shutil.rmtree(path, ignore_errors=True)
    if os.path.lexists(path):
        print(f"{PROG}: could not remove {path}", file=sys.stderr)


def child_pids() -> list[int]:
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat") as f:
                stat_line = f.read()
        except OSError:
            continue
        # The command name may contain spaces and parentheses, so split after the last ')'
        if int(stat_line.rsplit(")", 1)[1].split()[1]) == os.getpid():
            pids.append(int(entry))
    return pids


def reap_descendants(grace: float = 2.0) -> None:
    """Terminates the step's leftover descendants, which the subreaper setting reparents here
    however they detach, giving them the grace period to exit on SIGTERM before SIGKILL."""
    deadline = time.monotonic() + grace
    terminated: set[int] = set()
    while True:
        try:
            while os.waitpid(-1, os.WNOHANG)[0]:
                pass
        except ChildProcessError:
            return
        kill = time.monotonic() >= deadline
        for pid in child_pids():
            if kill or pid not in terminated:
                terminated.add(pid)
                try:
                    os.kill(pid, signal.SIGKILL if kill else signal.SIGTERM)
                except ProcessLookupError:
                    pass
        time.sleep(0.05)


def run_landlock(
    opts: Options, root: str, workdir: str, hides: list[str], mounts: list[Mount]
) -> int:
    landlock = Landlock()
    scratch = tempfile.mkdtemp(prefix="landlock.")
    child: subprocess.Popen[bytes] | None = None

    # Forwarded so the child exits first and the scratch directory is still removed
    def forward(signum: int, _frame: object) -> None:
        if child is None:
            sys.exit(128 + signum)
        child.send_signal(signum)

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, forward)
    try:
        os.mkdir(f"{scratch}/home")
        os.mkdir(f"{scratch}/tmp")
        policy, sandbox_env = landlock_policy(
            root, workdir, hides, mounts, scratch, dict(os.environ)
        )
        ruleset = landlock.ruleset(policy.rules())
        env_specs = [*opts.env, *(f"{k}={v}" for k, v in sandbox_env.items())]
        echo(opts, "LANDLOCK", opts.command, ["env", *env_specs])
        if landlock.libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0):
            die(f"PR_SET_CHILD_SUBREAPER: {os.strerror(ctypes.get_errno())}")
        parent = os.getpid()
        try:
            child = subprocess.Popen(
                opts.command,
                cwd=workdir,
                env=with_env(env_specs),
                preexec_fn=lambda: landlock.restrict_self(ruleset, parent),
            )
        except OSError as e:
            die_exec(opts.command[0], e)
        status = child.wait()
        reap_descendants()
        return 128 - status if status < 0 else status
    finally:
        remove_tree(scratch)


# Docker


def docker_args(
    opts: Options,
    root: str,
    workdir: str,
    hides: list[str],
    mounts: list[Mount],
    environ: dict[str, str],
) -> list[str]:
    """Builds the `docker run` arguments up to and including the image."""
    # The gateway alias (host.docker.internal, from --add-host below) is host-local, so it
    # must never go through a proxy
    no_proxy = environ.get("no_proxy") or environ.get("NO_PROXY") or ""
    no_proxy = f"{no_proxy},host.docker.internal" if no_proxy else "host.docker.internal"
    docker = f"{root}/docker"
    args = [
        *("--rm", "--init"),
        *("--env", "https_proxy", "--env", "http_proxy"),
        *("--env", "HTTPS_PROXY", "--env", "HTTP_PROXY"),
        *("--env", f"no_proxy={no_proxy}", "--env", f"NO_PROXY={no_proxy}"),
        *("--env", "OPENROUTER_API_KEY", "--env", "OPENAI_API_KEY"),
        *(
            "--env",
            "ANTHROPIC_API_KEY",
            "--env",
            "VLLM_API_KEY",
            "--env",
            "AZURE_OPENAI_API_KEY",
        ),
        *("--env", "TERM", "--env", "NO_COLOR"),
        *("--env", "UV_NO_SYNC=1", "--env", "TINI_KILL_PROCESS_GROUP=1"),
        *("--add-host", "host.docker.internal:host-gateway"),
        *("--workdir", workdir),
        *("--mount", f"type=bind,src={root},dst={root},readonly"),
        *("--mount", f"type=bind,src={docker}/venv,dst={root}/.venv"),
        *("--mount", f"type=bind,src={docker}/uv/data,dst=/home/user/.local/share/uv"),
        *("--mount", f"type=bind,src={docker}/uv/cache,dst=/home/user/.cache/uv"),
        *("--mount", f"type=bind,src={docker}/cargo/git,dst=/usr/local/cargo/git"),
        *("--mount", f"type=bind,src={docker}/cargo/registry,dst=/usr/local/cargo/registry"),
    ]
    for path in hides:
        args += ["--mount", f"type=tmpfs,dst={path}"]
    for spec in opts.env:
        args += ["--env", spec]
    if workdir != root and not any(m.path == workdir for m in mounts):
        args += ["--mount", f"type=tmpfs,dst={workdir}"]
    for mount in mounts:
        readonly = "" if mount.writable else ",readonly"
        args += ["--mount", f"type=bind,src={mount.path},dst={mount.path}{readonly}"]
    if opts.tty:
        args.append("-it")
    elif os.isatty(1) and os.isatty(2) and not environ.get("NO_COLOR"):
        # Container output is a pipe, so pass on the host terminal's colour and width
        columns = shutil.get_terminal_size((120, 0)).columns
        args += ["--env", "FORCE_COLOR=1", "--env", f"COLUMNS={columns}"]
    args.append(opts.image)
    return args


def run_docker(
    opts: Options, root: str, workdir: str, hides: list[str], mounts: list[Mount]
) -> NoReturn:
    for path in ("uv/cache", "uv/data", "venv", "cargo/git", "cargo/registry"):
        os.makedirs(f"{root}/docker/{path}", exist_ok=True)
    # The mountpoint must exist since the root is bind-mounted read-only
    os.makedirs(f"{root}/.venv", exist_ok=True)

    args = docker_args(opts, root, workdir, hides, mounts, dict(os.environ))
    details = ["docker", "run", *args] if os.environ.get("VERBOSE") == "1" else []
    echo(opts, f"DOCKER {opts.image}", opts.command, details)
    if opts.inspect:
        args += ["bash", "-c", 'bash -i; exec "$@"', "--"]
    try:
        os.execvp("docker", ["docker", "run", *args, *opts.command])
    except OSError as e:
        die_exec("docker", e)


def main(argv: list[str]) -> int:
    opts = parse_args(argv)
    if opts.runner == "native":
        run_native(opts)
    if not opts.root:
        usage()
    root = resolve_dir(opts.root, "root directory")
    workdir = resolve_dir(opts.workdir, "working directory") if opts.workdir else root
    hides = resolve_hides(root, opts.hides)
    mounts = resolve_mounts(root, opts.mounts)
    if opts.runner == "landlock":
        return run_landlock(opts, root, workdir, hides, mounts)
    run_docker(opts, root, workdir, hides, mounts)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
