#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import re
import json
import shutil
import signal

import tomlkit
import logging
import subprocess
from collections.abc import Iterable, Iterator, Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from functools import cache, cached_property
from typing import Any, Literal
from tempfile import TemporaryDirectory
from pathlib import Path

from .ast_rust import CodeRust


logger = logging.getLogger("ideas.tools")

ANSI = re.compile(r"\x1b\[[0-9;]*m")
# A rapx finding is a log line naming the bug followed by an annotate-snippets location
RAPX_BUG = re.compile(
    r"\|RAPx\|WARN\|: warning: (?P<bug>[^\n]+?)\.?\n\s*-+> (?P<file>[^\n:]+):(?P<line>\d+):"
)
RAPX_NOISE = re.compile(r"\s*\bdetected\b")


class VCS:
    def __init__(
        self,
        repo_dir: Path,
        vcs: Literal["none", "git"] = "git",
    ):
        self.repo_dir = repo_dir
        self.vcs = vcs

    def init(self, force_init: bool = False) -> bool:
        if self.vcs == "none":
            return True

        if not force_init:
            ok, _ = self("rev-parse --abbrev-ref HEAD")
            if ok:
                return True

        for cmd in ("init --initial-branch=main", f"config core.excludesFile {os.devnull}"):
            ok, out = self(cmd)
            if not ok:
                raise ValueError(f"Failed to initialize git in {self.repo_dir}!\n{out}")
        return True

    def add(self, *paths: Path, force: bool = False) -> bool:
        if self.vcs == "none":
            return True

        ok = True
        for path in paths:
            ok, out = self(f"add {'-f ' if force else ''}{path}")
            if not ok:
                raise ValueError(f"Failed to add {path}!\n{out}")
        return ok

    def rm(self, *paths: Path, force: bool = False) -> bool:
        if self.vcs == "none":
            ret = True
            for path in paths:
                target = path if path.is_absolute() else self.repo_dir / path
                try:
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                except Exception:
                    if not force:
                        ret = False
            return ret

        ret = True
        for path in paths:
            cmd = ["rm"]
            if path.is_dir():
                cmd.append("-r")
            if force:
                cmd.append("-f")

            ok, out = self(" ".join([*cmd, str(path)]))
            if not force:
                ret = ret and ok
        return ret

    def commit(self, message: str = "") -> bool:
        if self.vcs == "none":
            return True

        message = message.replace("\x00", "")
        ok, out = self("commit --allow-empty -F -", input=message)
        if not ok:
            raise ValueError(f"Failed to commit changes to git!\n{out}")
        return ok

    def __call__(self, cmd, *args, **kwargs) -> tuple[bool, str]:
        if self.vcs == "none":
            return True, ""

        success, output, error, _ = run_subprocess(
            [self.vcs, "-C", str(self.repo_dir), *cmd.split(" "), *args], **kwargs
        )
        return success, output + error


class Workspace:
    def __init__(self, root: Path, vcs: Literal["none", "git"] = "none"):
        self.root = root
        self.vcs = VCS(repo_dir=root, vcs=vcs)

    @property
    def cargo_toml(self) -> Path:
        return self.root / "Cargo.toml"

    @property
    def cargo_lock(self) -> Path:
        return self.root / "Cargo.lock"

    def stage(self) -> None:
        self.vcs.add(*(path for path in (self.cargo_toml, self.cargo_lock) if path.exists()))

    def _member(self, crate: "Crate") -> str:
        return str(crate.cargo_toml.parent.relative_to(self.root))

    def add_default_member(self, crate: "Crate") -> None:
        manifest = tomlkit.loads(self.cargo_toml.read_text())
        default_members = manifest["workspace"].setdefault("default-members", tomlkit.array())  # type: ignore[union-attr]
        if (member := self._member(crate)) not in default_members:
            default_members.append(member)
            self.cargo_toml.write_text(tomlkit.dumps(manifest))
            self.vcs.add(self.cargo_toml)

    def remove_member(self, crate: "Crate") -> None:
        manifest = tomlkit.loads(self.cargo_toml.read_text())
        manifest["workspace"]["members"].remove(self._member(crate))  # type: ignore[union-attr]
        self.cargo_toml.write_text(tomlkit.dumps(manifest))

        crate_dir = crate.cargo_toml.parent
        self.vcs.rm(crate_dir, force=True)
        shutil.rmtree(crate_dir, ignore_errors=True)

        # Re-resolving the workspace drops the member from Cargo.lock
        success, output, error, _ = run_subprocess(
            ["cargo", "metadata", "--format-version=1", "--manifest-path", str(self.cargo_toml)]
        )
        if not success:
            raise ValueError(f"Failed to re-resolve workspace {self.root}!\n{output + error}")
        self.stage()

    def clean(self) -> None:
        cmd = ["cargo", "clean", "--quiet", f"--manifest-path={self.cargo_toml}"]
        success, output, error, _ = run_subprocess(cmd)
        if not success:
            logger.warning(f"Failed to clean workspace at {self.root}:\n\n{output + error}")


class Crate:
    def __init__(
        self,
        cargo_toml: Path,
        vcs: Literal["none", "git"] = "none",
        template: Literal["bin", "lib"] | None = None,
        reinit: bool = False,
        *,
        jobs: int | None = 1,
    ):
        self.cargo_toml = cargo_toml.resolve()
        self.jobs = jobs

        crate_dir = self.cargo_toml.parent
        self.vcs = VCS(repo_dir=crate_dir, vcs=vcs)

        if reinit and self.cargo_toml.exists():
            self.cargo_toml.unlink()

        if not self.cargo_toml.exists():
            # Create a new crate with specified template, but without VCS
            if not template:
                raise ValueError(
                    f"Crate at {crate_dir} does not exist; template must be specified!"
                )
            os.makedirs(crate_dir, exist_ok=True)
            success, output, error, _ = run_subprocess(
                [
                    "cargo",
                    "init",
                    "--quiet",
                    f"--{template}",
                    "--vcs=none",
                    str(crate_dir),
                ]
            )
            if not success:
                raise RuntimeError(
                    f"Failed to create new crate at {crate_dir} with error:\n\n{output + error}"
                )

        # Initialize repository if needed
        self.vcs.init()

    @cached_property
    def metadata(self) -> dict[str, Any]:
        success, out, error, _ = run_subprocess(
            ["cargo", "metadata", "--manifest-path", str(self.cargo_toml)],
        )
        if not success:
            raise ValueError(
                f"Failed to get cargo metadata from {self.cargo_toml}!\n{out + error}"
            )
        metadata = json.loads(out)
        return metadata

    def invalidate_metadata(self) -> None:
        if "metadata" in self.__dict__:
            del self.metadata

    @property
    def root_package(self) -> dict[str, Any]:
        root = self.metadata["resolve"]["root"]
        if root is None:
            if len(self.metadata["workspace_members"]) != 1:
                raise ValueError("No root package specified!")
            root = self.metadata["workspace_members"][0]

        root_package = next(filter(lambda p: p["id"] == root, self.metadata["packages"]))
        return root_package

    def dependency_packages(self, names: Iterable[str]) -> dict[str, dict[str, Any]]:
        packages = {p["id"]: p for p in self.metadata["packages"]}
        root = self.root_package["id"]
        node = next(n for n in self.metadata["resolve"]["nodes"] if n["id"] == root)
        # Only normal deps: dev/build deps are not available to the library's code
        direct = {
            packages[d["pkg"]]["name"]: packages[d["pkg"]]
            for d in node["deps"]
            if any(k["kind"] is None for k in d["dep_kinds"])
        }
        return {name: direct[name] for name in names}

    @property
    def workspace(self) -> Workspace:
        root = self.metadata.get("workspace_root", None)
        # Standalone crates without workspace metadata fall back to crate directory.
        root = self.cargo_toml.parent if root is None else Path(root)
        return Workspace(root, vcs=self.vcs.vcs)  # type: ignore[reportArgumentType]

    @property
    def bin_targets(self) -> list[dict[str, Any]]:
        return list(filter(lambda t: "bin" in t["kind"], self.root_package["targets"]))

    @property
    def lib_target(self) -> dict[str, Any] | None:
        all_targets = list(filter(lambda t: "lib" in t["kind"], self.root_package["targets"]))
        if len(all_targets) == 1:
            return all_targets[0]
        if len(all_targets) == 0:
            return None
        raise ValueError(f"Multiple lib targets found in Cargo.toml: {self.cargo_toml=}")

    @property
    def lib_name(self) -> str | None:
        if self.lib_target is None:
            return None
        return self.lib_target["name"]

    @property
    def lib_src_path(self) -> Path | None:
        if self.lib_target is None:
            return None
        return Path(self.lib_target["src_path"])

    @property
    def main_src_path(self) -> Path | None:
        if len(self.bin_targets) == 1:
            return Path(self.bin_targets[0]["src_path"])
        if len(self.bin_targets) > 1:
            raise NotImplementedError(
                f"Multiple bin targets found in Cargo.toml: {self.cargo_toml=} {self.bin_targets=}"
            )
        return None

    @property
    def name(self) -> str:
        return self.root_package["name"]

    def cargo_add(
        self,
        dep: str,
        section: str | None = None,
        features: list[str] | None = None,
        copy_from: Path | None = None,
    ) -> str:
        source = copy_from or self.workspace.cargo_toml
        manifest = tomlkit.loads(source.read_text()).unwrap()
        # A bare name missing from the workspace would silently fall back to the registry
        if (spec := manifest.get("workspace", {}).get("dependencies", {}).get(dep)) is None:
            raise ValueError(f"`{dep}` is not in [workspace.dependencies] of {source}")
        if copy_from is not None:
            spec = {"version": spec} if isinstance(spec, str) else spec
            dep = f"{dep}@{spec['version']}"
            features = [*spec.get("features", []), *(features or [])]
        cmd = [
            "cargo",
            "add",
            "--quiet",
            f"--manifest-path={self.cargo_toml}",
        ]
        if section:
            cmd.append(f"--{section}")
        if features:
            cmd.append(f"--features={','.join(features)}")
        cmd.append(dep)

        success, output, error, _ = run_subprocess(cmd)
        if not success:
            raise RuntimeError(
                f"Failed to add dependency {dep} to {self.cargo_toml} with error:\n\n{output + error}"
            )

        # Invalidate cached metadata
        self.invalidate_metadata()
        return output

    def cargo_feature(self, **features: list[str]) -> None:
        # Set/overwrite new features
        cargo_toml = tomlkit.loads(self.cargo_toml.read_text())
        cargo_features = cargo_toml.get("features", {})
        for name, deps in features.items():
            cargo_features[name] = deps
        cargo_toml["features"] = cargo_features
        self.cargo_toml.write_text(tomlkit.dumps(cargo_toml))

        # Invalidate cached metadata
        self.invalidate_metadata()

    def configure_target(
        self,
        section: Literal["bin", "lib"],
        name: str | None = None,
        test: bool = True,
        doctest: bool = True,
        crate_type: list[str] | None = None,
        path: str | None = None,
    ) -> None:
        content = tomlkit.loads(self.cargo_toml.read_text())
        table = tomlkit.table()
        if name is not None:
            table.add("name", name)
        if path is not None:
            table.add("path", path)
        if crate_type is not None:
            table.add("crate-type", crate_type)
        table.add("test", test)
        table.add("doctest", doctest)
        # A crate can have multiple binaries, so [[bin]] must be an array of tables
        if section == "bin":
            aot = tomlkit.aot()
            aot.append(table)
            table = aot
        content[section] = table
        self.cargo_toml.write_text(tomlkit.dumps(content))
        self.invalidate_metadata()

    def add_workspace_dependencies(
        self, names: list[str | Mapping[str, str]], *, dev: bool = False
    ) -> None:
        # A mapping names a package by alias only, e.g. `{"safe_crate": "libfoo-rs"}`
        deps: list[tuple[str | None, str]] = []
        for entry in names:
            if isinstance(entry, str):
                deps.append((None, entry.strip()))
            else:
                deps.extend((alias.strip(), name.strip()) for alias, name in entry.items())
        if not deps:
            return
        section = "dev-dependencies" if dev else "dependencies"
        workspace_root = self.workspace.root
        workspace_toml_path = workspace_root / "Cargo.toml"
        workspace_toml = tomlkit.loads(workspace_toml_path.read_text())
        workspace_table = workspace_toml.get("workspace", tomlkit.table())
        workspace_deps = workspace_table.get("dependencies", tomlkit.table())

        workspace_member_ids = set(self.metadata.get("workspace_members", []))
        workspace_packages = {
            pkg["name"]: pkg
            for pkg in self.metadata.get("packages", [])
            if pkg.get("id") in workspace_member_ids
        }

        cargo_toml = tomlkit.loads(self.cargo_toml.read_text())
        dependencies = cargo_toml.get(section, tomlkit.table())

        for alias, dep_name in deps:
            if not dep_name or alias == "":
                raise ValueError("workspace dependency names and aliases must be non-empty")

            pkg = workspace_packages.get(dep_name)
            if pkg is None:
                raise ValueError(
                    f"Workspace dependency `{dep_name}` was not found among workspace members"
                )

            manifest_path = Path(pkg["manifest_path"])
            dep_dir = manifest_path.parent
            rel_path = dep_dir.relative_to(workspace_root)
            workspace_deps[dep_name] = {"path": str(rel_path)}
            if alias is None:
                dependencies[dep_name] = {"workspace": True}
            else:
                # rustc only gets `--extern {alias}` so no leaked names are allowed
                dependencies[alias] = {
                    "path": os.path.relpath(dep_dir, self.cargo_toml.parent),
                    "package": dep_name,
                }

        workspace_table["dependencies"] = workspace_deps
        workspace_toml["workspace"] = workspace_table
        workspace_toml_path.write_text(tomlkit.dumps(workspace_toml))

        cargo_toml[section] = dependencies
        self.cargo_toml.write_text(tomlkit.dumps(cargo_toml))
        self.invalidate_metadata()

    def cargo_build(self) -> tuple[bool, str]:
        cmd = [
            "cargo",
            "build",
            "--quiet",
            "--color=never",
            f"--manifest-path={self.cargo_toml}",
        ]
        if self.jobs is not None:
            cmd.extend(["-j", str(self.jobs)])

        builds, output, error, _ = run_subprocess(cmd, env=self._cargo_env())
        return builds, output + error

    def _cargo_env(self) -> dict[str, str]:
        env = os.environ.copy()
        if self.jobs is not None:
            env["CARGO_BUILD_JOBS"] = str(self.jobs)
            env["RUST_TEST_THREADS"] = str(self.jobs)
            env["NEXTEST_TEST_THREADS"] = str(self.jobs)
        return env

    def cargo_clippy(
        self, *clippy_args: str, targets: str | tuple[str, ...] = "--lib"
    ) -> "ClippyResult":
        if isinstance(targets, str):
            targets = (targets,)
        cmd = [
            "cargo",
            "clippy",
            "--quiet",
            "--color=never",
            f"--manifest-path={self.cargo_toml}",
            "--message-format=json",
            "--no-deps",
            *targets,
        ]
        if self.jobs is not None:
            cmd.extend(["-j", str(self.jobs)])
        if clippy_args:
            cmd.extend(["--", *clippy_args])

        _, output, error, returncode = run_subprocess(cmd, env=self._cargo_env())
        return ClippyResult.from_json(
            output,
            error,
            returncode,
            package_id=self.root_package["id"],
            root=self.workspace.root,
            lib_src_path=self.lib_src_path,
        )

    def cargo_rapx(self, toolchain: str, timeout: int | None = None) -> list[dict[str, Any]]:
        # rapx links against rustc internals, so it runs on the nightly it was built for
        cmd = ["cargo", f"+{toolchain}", "rapx"]
        if timeout is not None:
            cmd.extend(["--timeout", str(timeout)])
        # `-f` only: `-m` reports leaks, which are safe in Rust and so not UB-adjacent
        cmd.extend(["check", "-f"])

        env = self._cargo_env()
        # Cleaning would evict the build cache the pipeline shares, and a separate target dir
        # keeps the stable and nightly rustcs from invalidating each other
        env["RAPX_CLEAN"] = "false"
        env["RAPX_RECURSIVE"] = "none"
        env.setdefault("CARGO_TARGET_DIR", str(self.workspace.root / "target" / "rapx"))

        # rapx analyses the package at the cwd; `--manifest-path` only reaches `cargo check`
        success, output, error, reason = run_subprocess(
            cmd,
            cwd=self.cargo_toml.parent,
            env=env,
            # Margin over rapx's own deadline so it gets to bail out and report first
            timeout=None if timeout is None else timeout + 60,
        )
        if not success:
            raise RuntimeError(
                f"`cargo rapx` failed on {self.cargo_toml} ({reason}):\n\n{output + error}"
            )

        return [
            {
                "bug": RAPX_NOISE.sub("", match["bug"]).strip(),
                "file": match["file"],
                "line": int(match["line"]),
            }
            # rapx has no machine-readable output; it renders findings to stderr
            for match in RAPX_BUG.finditer(ANSI.sub("", error))
        ]

    def cargo_test_list(self, name: str, features: list[str] | None = None) -> list[str]:
        cmd = [
            "cargo",
            "nextest",
            "list",
            "--color=never",
            "--cargo-quiet",
            f"--manifest-path={self.cargo_toml}",
            "--message-format=json",
        ]
        if name:
            cmd.extend(["--test", name])
        if features:
            cmd.extend(["--features", ",".join(features)])

        ok, output, error, _ = run_subprocess(cmd, env=self._cargo_env())
        if not ok:
            raise RuntimeError(
                f"Failed to list the tests of `{name}` in {self.cargo_toml.parent}!\n{output + error}"
            )
        listing = json.loads(output)
        return [
            test for suite in listing["rust-suites"].values() for test in suite["testcases"]
        ]

    def cargo_test(
        self,
        name: str,
        quiet: bool = True,
        fail_fast: bool = False,
        build_only: bool = False,
        skip: list[str] | None = None,
        message_format: str | None = None,
        lib: bool = False,
    ) -> tuple[bool, str, str, int | Literal["timeout"]]:
        cmd = [
            "cargo",
            "nextest",
            "run",
            "--color=never",
            f"--manifest-path={self.cargo_toml}",
        ]
        if self.jobs is not None:
            cmd.extend(["--build-jobs", str(self.jobs)])
        if not fail_fast and not build_only:
            cmd.append("--no-fail-fast")
        if quiet:
            cmd.append("--cargo-quiet")
        if lib:
            cmd.append("--lib")
            if name:
                cmd.append(name)  # positional substring filter
        elif name:
            cmd.extend(["--test", name])  # integration test binary
        if build_only:
            cmd.append("--no-run")

        env = self._cargo_env()
        rustflags = env.get("RUSTFLAGS", "")
        if "-Awarnings" not in rustflags.split():
            env["RUSTFLAGS"] = f"{rustflags} -Awarnings".strip()
        if message_format is not None:
            cmd.extend(["--message-format", message_format])
            if message_format == "libtest-json":
                # https://nexte.st/docs/machine-readable/libtest-json/
                env["NEXTEST_EXPERIMENTAL_LIBTEST_JSON"] = "1"
        if skip:
            excluded_tests = [f"test(/^{re.escape(test_name)}$/)" for test_name in skip]
            expr = " and ".join(f"not {test_expr}" for test_expr in excluded_tests)
            cmd.extend(["-E", expr])
            cmd.append("--no-tests=pass")
        return run_subprocess(cmd, env=env)

    def cargo_nextest_config(
        self, nextest_config_path: Path, slow: int = 30, terminate_after: int = 2
    ) -> None:
        nextest_config_path.parent.mkdir(parents=True, exist_ok=True)
        nextest_config = {
            "profile": {
                "default": {
                    "slow-timeout": {"period": f"{slow}s", "terminate-after": terminate_after},
                    "test-threads": 1,
                }
            }
        }
        nextest_config_path.write_text(tomlkit.dumps(nextest_config))
        self.invalidate_metadata()

    def write(self, path: Path, data, **kwargs):
        if path.is_absolute():
            raise ValueError("path must not be absolute")
        path = self.cargo_toml.parent / path
        return path.write_text(data, **kwargs)


@dataclass(frozen=True)
class ClippyResult:
    success: bool
    # This package's diagnostics that point somewhere
    diagnostics: list[dict[str, Any]]
    # Errors left out of `diagnostics`, which still fail Clippy
    errors: list[str]
    stderr: str
    root: Path
    lib_src_path: Path | None

    @classmethod
    def from_json(
        cls,
        stdout: str,
        stderr: str,
        returncode: int | Literal["timeout"],
        *,
        package_id: str,
        root: Path,
        lib_src_path: Path | None,
    ) -> "ClippyResult":
        # Path dependencies (`--no-deps` skips only their Clippy lints) and span-less summaries
        # are not this package's to fix, but their errors still fail clippy
        diagnostics: dict[str, dict[str, Any]] = {}
        errors: dict[str, None] = {}
        for line in stdout.splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("reason") != "compiler-message":
                continue
            if not isinstance(message := record.get("message"), dict):
                continue
            if record.get("package_id") == package_id and _primary_spans(message):
                key = message.get("rendered") or json.dumps(message, sort_keys=True)
                diagnostics.setdefault(key, message)
            elif _is_error(message):
                errors[_text(message)] = None

        success = returncode == 0
        stderr = stderr.strip()
        package_errors = any(_is_error(d) for d in diagnostics.values())
        if not success and not errors and not stderr and not package_errors:
            stderr = f"`cargo clippy` failed ({returncode}) without saying why"
        return cls(
            success=success,
            diagnostics=list(diagnostics.values()),
            errors=list(errors),
            stderr=stderr,
            root=root,
            lib_src_path=lib_src_path,
        )

    @property
    def output(self) -> str:
        return "\n\n".join(text for text in [*self.errors, self.stderr] if text)

    @property
    def has_errors(self) -> bool:
        return any(_is_error(d) for d in self.diagnostics)

    def render(
        self, levels: tuple[str, ...] = ("warning", "error"), *, failure: bool = True
    ) -> str:
        # Levels match by prefix, so "error" also covers "error: internal compiler error"
        relevant = [d for d in self.diagnostics if str(d.get("level", "")).startswith(levels)]
        texts = [_text(d) for d in relevant]
        if failure and not self.success:
            texts.append(self.output.strip())
        rendered = "\n\n".join(text for text in texts if text)
        if relevant and not rendered:
            rendered = "Clippy reported a warning or error without diagnostic details."
        return rendered

    def only_in(self, code: CodeRust) -> "ClippyResult":
        # Code the attempt may not edit, like bindgen's bitfield accessors, can carry warnings
        # it could never fix
        # FIXME: Scope to the whole package, to catch warnings attempts cause in earlier code,
        # once generated code is clippy-clean
        kept, outside = self._split(code)
        errors = {_text(d): None for d in outside if _is_error(d)}
        errors.update(dict.fromkeys(self.errors))
        # Errors elsewhere fail Clippy but not this scope; they stay in `output` for the caller
        success = self.success or (bool(errors) and not any(_is_error(d) for d in kept))
        return replace(self, success=success, diagnostics=kept, errors=list(errors))

    def not_in(self, code: CodeRust) -> "ClippyResult":
        # What `only_in` leaves out: the package's diagnostics outside `code`
        return replace(self, diagnostics=self._split(code)[1])

    def _split(self, code: CodeRust) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        assert self.lib_src_path is not None
        lib_src_path = self.lib_src_path.resolve()
        text, src = lib_src_path.read_text(), str(code)
        start = text.rfind(src) if src else -1
        # None of the file is the attempt's if it is not there, so none of it is held against it
        first = text.count("\n", 0, start) + 1
        lines = range(first, first + src.count("\n")) if start >= 0 else range(0)

        def in_code(diagnostic: dict[str, Any]) -> bool:
            return any(
                path == lib_src_path and line in lines
                for span in _primary_spans(diagnostic)
                for path, line in _span_locations(self.root, span)
            )

        inside = [d for d in self.diagnostics if in_code(d)]
        return inside, [d for d in self.diagnostics if not in_code(d)]


def _primary_spans(diagnostic: dict[str, Any]) -> list[dict[str, Any]]:
    return [s for s in diagnostic.get("spans") or [] if s.get("is_primary")]


def _is_error(diagnostic: dict[str, Any]) -> bool:
    return str(diagnostic.get("level", "")).startswith("error")


def _text(diagnostic: dict[str, Any]) -> str:
    return (diagnostic.get("rendered") or diagnostic.get("message", "")).rstrip()


def _span_locations(root: Path, span: dict[str, Any]) -> Iterator[tuple[Path, int]]:
    # Macro expansions report the definition site first, then each call site, innermost first
    while True:
        path = Path(span["file_name"])
        yield (path if path.is_absolute() else root / path).resolve(), span["line_start"]
        if not (expansion := span.get("expansion")):
            return
        span = expansion["span"]


def _kill_group(
    proc: "subprocess.Popen[str]", grace: float = 2.0, drain: float = 5.0
) -> tuple[str, str]:
    # SIGTERM first so the process group can clean up after itself, SIGKILL if it lingers
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
        with suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=grace)
        # FIXME: Give grace to the process group, not just leader, to exit cleanly
        os.killpg(proc.pid, signal.SIGKILL)

    # A descendant that left the process group survives the kill and holds the pipes open
    try:
        stdout, stderr = proc.communicate(timeout=drain)
    except subprocess.TimeoutExpired as e:
        stdout = e.stdout.decode(errors="replace") if e.stdout else ""
        stderr = e.stderr.decode(errors="replace") if e.stderr else ""
    return stdout, stderr


def run_subprocess(
    cmd: list[str],
    input: str | None = None,
    timeout: float | None = None,
    **kwargs,
) -> tuple[bool, str, str, int | Literal["timeout"]]:
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE if input is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        **kwargs,
    )
    with proc:
        try:
            stdout, stderr = proc.communicate(input, timeout=timeout)
        except subprocess.TimeoutExpired:
            stdout, stderr = _kill_group(proc)
            return False, stdout, stderr, "timeout"
        except BaseException:
            # start_new_session detaches the child from the tty, so Ctrl-C never reaches it
            _kill_group(proc)
            raise
    return proc.returncode == 0, stdout, stderr, proc.returncode


def check_rust(
    code: str,
    *,
    flags: list[str] | None = None,
    structured_output: bool = False,
) -> tuple[bool, str]:
    cmd = ["rustc", "--edition=2024", "-Awarnings"]

    if flags:
        cmd.extend(flags)

    if structured_output:
        cmd.append("--error-format=json")

    with TemporaryDirectory() as dirname:
        cmd.extend(["-", "--out-dir", dirname])

    success, output, error, _ = run_subprocess(cmd, input=code)
    return success, output + error


@cache
def rustc_version() -> tuple[int, int, int]:
    ok, output, error, _ = run_subprocess(["rustc", "--version"])
    if not ok or (match := re.match(r"rustc (\d+)\.(\d+)\.(\d+)", output)) is None:
        raise RuntimeError(f"`rustc --version` failed!\n{output}{error}")
    return int(match[1]), int(match[2]), int(match[3])


def rustfmt(path: Path) -> None:
    cmd = ["rustfmt", str(path)]
    run_subprocess(cmd)
