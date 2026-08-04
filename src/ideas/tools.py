#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import re
import json
import shutil

import tomlkit
import logging
import subprocess
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

        ok, out = False, ""
        if not force_init:
            ok, out = self("rev-parse --abbrev-ref HEAD")
        if not ok:
            ok, out = self("init --initial-branch=main")
        if not ok:
            raise ValueError(f"Failed to initialize git in {self.repo_dir}!\n{out}")
        return ok

    def add(self, *paths: Path) -> bool:
        if self.vcs == "none":
            return True

        ok = True
        for path in paths:
            ok, out = self(f"add {path}")
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
    ):
        self.cargo_toml = cargo_toml.resolve()

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

    def cargo_clean(self) -> None:
        cmd = [
            "cargo",
            "clean",
            "--quiet",
            f"--manifest-path={self.cargo_toml}",
        ]
        success, output, error, _ = run_subprocess(cmd)
        if not success:
            raise RuntimeError(
                f"Failed to clean crate at {self.cargo_toml} with error:\n\n{output + error}"
            )

    def cargo_build(self) -> tuple[bool, str]:
        cmd = [
            "cargo",
            "build",
            "--quiet",
            "--color=never",
            f"--manifest-path={self.cargo_toml}",
        ]
        builds, output, error, _ = run_subprocess(cmd)
        return builds, output + error

    def cargo_test(
        self,
        name: str,
        test_harness: Literal["nextest run", "test"] = "nextest run",
        quiet: bool = True,
        fail_fast: bool = False,
        build_only: bool = False,
        skip: list[str] | None = None,
        message_format: str | None = None,
        lib: bool = False,
    ) -> tuple[bool, str, str, int | Literal["timeout"]]:
        cmd = [
            "cargo",
            *test_harness.split(),
            "--color=never",
            f"--manifest-path={self.cargo_toml}",
        ]
        if not fail_fast:
            cmd.append("--no-fail-fast")
        if quiet:
            if test_harness == "nextest run":
                cmd.append("--cargo-quiet")
            elif test_harness == "test":
                cmd.append("--quiet")
            else:
                raise ValueError(f"Unsupported test harness: {test_harness}")
        if lib:
            cmd.append("--lib")
            if name:
                cmd.append(name)  # positional substring filter
        elif name:
            cmd.extend(["--test", name])  # integration test binary
        if build_only:
            cmd.append("--no-run")

        env = os.environ.copy()
        if message_format is not None:
            cmd.extend(["--message-format", message_format])
            if message_format == "libtest-json" and test_harness == "nextest run":
                # https://nexte.st/docs/machine-readable/libtest-json/
                env["NEXTEST_EXPERIMENTAL_LIBTEST_JSON"] = "1"
        if skip:
            if test_harness == "nextest run":
                excluded_tests = [f"test(/^{re.escape(test_name)}$/)" for test_name in skip]
                expr = " and ".join(f"not {test_expr}" for test_expr in excluded_tests)
                cmd.extend(["-E", expr])
            else:
                cmd.append("--")
                cmd.append("--exact")
                for test_name in skip:
                    cmd.extend(["--skip", test_name])
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


def run_subprocess(
    cmd: list[str],
    input: str | None = None,
    timeout: float | None = None,
    **kwargs,
) -> tuple[bool, str, str, int | Literal["timeout"]]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            input=input,
            timeout=timeout,
            **kwargs,
        )
        return True, result.stdout, result.stderr, result.returncode
    except subprocess.CalledProcessError as e:
        return False, e.stdout, e.stderr, e.returncode
    except subprocess.TimeoutExpired as e:
        return (
            False,
            e.stdout.decode() if e.stdout else "",
            e.stderr.decode() if e.stderr else "",
            "timeout",
        )


def check_rust(
    code: str,
    *,
    flags: list[str] | None = None,
    structured_output: bool = False,
) -> tuple[bool, str]:
    cmd = ["rustc"]

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
