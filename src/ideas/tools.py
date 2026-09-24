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
from textwrap import dedent

import tomlkit
import logging
import subprocess
from contextlib import suppress
from functools import cached_property
from typing import Any, Literal
from tempfile import TemporaryDirectory
from pathlib import Path


logger = logging.getLogger("ideas.tools")


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


class Crate:
    def __init__(
        self,
        cargo_toml: Path,
        vcs: Literal["none", "git"] = "none",
        template: Literal["bin", "lib"] | None = None,
        reinit: bool = False,
        *,
        jobs: int | None = None,
        codegen_units: int | None = None,
    ):
        self.cargo_toml = cargo_toml.resolve()
        self.jobs = jobs
        self.codegen_units = codegen_units

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

    @property
    def workspace_root(self) -> Path:
        workspace_root = self.metadata.get("workspace_root", None)
        if workspace_root is None:
            # Standalone crates without workspace metadata fall back to crate directory.
            return self.cargo_toml.parent
        return Path(workspace_root)

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
    def src_dir(self) -> Path:
        src_paths: list[Path] = []
        if self.lib_src_path is not None:
            src_paths.append(self.lib_src_path.parent)
        if self.main_src_path is not None:
            src_paths.append(self.main_src_path.parent)

        if not src_paths:
            raise ValueError(f"Crate {self.name} has neither lib.rs nor main.rs!")
        if len({path.resolve() for path in src_paths}) > 1:
            raise ValueError(
                f"Crate {self.name} has inconsistent src directories: {src_paths!r}"
            )
        return src_paths[0]

    @property
    def name(self) -> str:
        return self.root_package["name"]

    def cargo_add(
        self, dep: str, section: str | None = None, features: list[str] | None = None
    ) -> str:
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

    # Workaround for https://github.com/rust-lang/cargo/issues/9454
    def add_self_alias(self, alias: str) -> None:
        lib_name = self.lib_name
        if lib_name is None:
            raise ValueError(f"Crate {self.name} has no library target to alias as `{alias}`!")

        shim_name = f"{self.name}-{alias.replace('_', '-')}"
        shim_dir = self.cargo_toml.parent / alias
        (shim_dir / "src").mkdir(parents=True, exist_ok=True)
        (shim_dir / "src" / "lib.rs").write_text(f"pub use {lib_name}::*;\n")
        (shim_dir / "Cargo.toml").write_text(
            dedent(f"""\
                [package]
                name = "{shim_name}"
                version = "0.1.0"
                edition = "{self.root_package["edition"]}"

                [dependencies]
                {lib_name} = {{ path = "..", package = "{self.name}" }}

                [lib]
                test = false
                doctest = false
            """)
        )

        # Add the shim crate as a fixed-name dev dependency in this crate
        manifest_path = Path(self.cargo_toml)
        doc = tomlkit.parse(manifest_path.read_text())
        dev_deps = doc.setdefault("dev-dependencies", tomlkit.table())
        entry = tomlkit.inline_table()
        entry["path"] = alias
        entry["package"] = shim_name
        dev_deps[alias] = entry
        manifest_path.write_text(tomlkit.dumps(doc))
        self.invalidate_metadata()

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
        name: str,
        test: bool = True,
        doctest: bool = True,
        crate_type: list[str] | None = None,
    ) -> None:
        content = tomlkit.loads(self.cargo_toml.read_text())
        table = tomlkit.table()
        table.add("name", name)
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

    def add_workspace_dependencies(self, names: list[str]) -> None:
        if not names:
            return

        workspace_root = self.workspace_root
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
        dependencies = cargo_toml.get("dependencies", tomlkit.table())

        for dep_name in names:
            dep_name = dep_name.strip()
            if not dep_name:
                raise ValueError("workspace dependency names must be non-empty")

            pkg = workspace_packages.get(dep_name)
            if pkg is None:
                raise ValueError(
                    f"Workspace dependency `{dep_name}` was not found among workspace members"
                )

            manifest_path = Path(pkg["manifest_path"])
            dep_dir = manifest_path.parent
            rel_path = dep_dir.relative_to(workspace_root)
            workspace_deps[dep_name] = {"path": str(rel_path)}
            dependencies[dep_name] = {"workspace": True}

        workspace_table["dependencies"] = workspace_deps
        workspace_toml["workspace"] = workspace_table
        workspace_toml_path.write_text(tomlkit.dumps(workspace_toml))

        cargo_toml["dependencies"] = dependencies
        self.cargo_toml.write_text(tomlkit.dumps(cargo_toml))
        self.invalidate_metadata()

    def cargo_clean(self, workspace: bool = False) -> None:
        manifest_path = self.workspace_root / "Cargo.toml" if workspace else self.cargo_toml
        cmd = [
            "cargo",
            "clean",
            "--quiet",
            f"--manifest-path={manifest_path}",
        ]
        success, output, error, _ = run_subprocess(cmd)
        if not success:
            logger.warning(f"Failed to clean crate at {manifest_path}:\n\n{output + error}")

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
        if self.codegen_units is not None:
            rustflags = env.get("RUSTFLAGS", "")
            env["RUSTFLAGS"] = (rustflags + f" -C codegen-units={self.codegen_units}").strip()
        return env

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

        ok, output, error, _ = run_subprocess(cmd)
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
    cmd = ["rustc", "-Awarnings"]

    if flags:
        cmd.extend(flags)

    if structured_output:
        cmd.append("--error-format=json")

    with TemporaryDirectory() as dirname:
        cmd.extend(["-", "--out-dir", dirname])

    success, output, error, _ = run_subprocess(cmd, input=code)
    return success, output + error


def rustfmt(path: Path) -> None:
    cmd = ["rustfmt", str(path)]
    run_subprocess(cmd)


MAX_DEPENDENT_CHARS = int(os.environ.get("MAX_DEPENDENT_CHARS", "20000"))
REDUCED_CONTEXT = os.environ.get("REDUCED_CONTEXT", "1") not in ("0", "", "false", "False")
