#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import shutil
import subprocess
from pathlib import Path

import pytest
import tomlkit

from ideas.hybrid import SAFE_CRATE
from ideas.tools import BaseWorkspace, Crate
from ideas.translate import TranslationWorkspace

# Dependency-free crates that are already in the cargo cache
WORKSPACE = """\
[workspace]
resolver = "3"

[workspace.dependencies]
libc = { version = "0.2", features = ["extra_traits"] }
memchr = "2"
"""


@pytest.fixture
def workspace(tmp_path, monkeypatch) -> Path:
    monkeypatch.setenv("CARGO_NET_OFFLINE", "true")
    (tmp_path / "Cargo.toml").write_text(WORKSPACE)
    return tmp_path


def _section(crate: Crate, section: str) -> dict:
    return tomlkit.loads(crate.cargo_toml.read_text()).unwrap().get(section, {})


@pytest.mark.parametrize("target", ["driver", "parser-rs", "parser-ffi"])
@pytest.mark.parametrize("has_ffi", [False, True])
def test_open_workspace_strips_exactly_one_suffix(workspace, target, has_ffi):
    sys_crate = Crate(workspace / f"{target}-sys" / "Cargo.toml", template="lib")
    rs_crate = Crate(workspace / f"{target}-rs" / "Cargo.toml", template="lib")
    ffi_toml = workspace / f"{target}-ffi" / "Cargo.toml"
    if has_ffi:
        Crate(ffi_toml, template="lib")

    opened = BaseWorkspace.open(rs_crate.cargo_toml)

    assert opened.target == target
    assert opened.sys_crate.cargo_toml == sys_crate.cargo_toml
    assert opened.rs_crate.cargo_toml == rs_crate.cargo_toml
    if has_ffi:
        assert opened.ffi_crate is not None
        assert opened.ffi_crate.cargo_toml == ffi_toml
    else:
        assert opened.ffi_crate is None


@pytest.mark.parametrize("crate", ["driver-ffi", "driver-sys", "driver-oxide", "driver"])
def test_open_workspace_rejects_non_rs_manifests(workspace, crate):
    with pytest.raises(ValueError, match="not the manifest of an -rs crate"):
        BaseWorkspace.open(workspace / crate / "Cargo.toml")


def test_plain_add_inherits_from_the_workspace(workspace):
    crate = Crate(workspace / "foo-rs" / "Cargo.toml", template="lib")

    crate.cargo_add("libc", section="dev")

    assert _section(crate, "dev-dependencies") == {"libc": {"workspace": True}}


def test_copy_inlines_the_workspace_spec_and_resolves_outside_it(workspace, tmp_path_factory):
    crate = Crate(workspace / "foo-sys" / "Cargo.toml", template="lib")

    crate.cargo_add("libc", section="dev", copy_from=workspace / "Cargo.toml")
    crate.cargo_add("memchr", section="build", copy_from=workspace / "Cargo.toml")

    assert _section(crate, "dev-dependencies") == {
        "libc": {"version": "0.2", "features": ["extra_traits"]}
    }
    assert _section(crate, "build-dependencies") == {"memchr": "2"}

    # testgen isolates -sys, so it must not refer back to the workspace
    isolated = tmp_path_factory.mktemp("isolated") / "foo-sys"
    shutil.copytree(crate.cargo_toml.parent, isolated)
    proc = subprocess.run(
        ["cargo", "metadata", "--format-version=1", "--manifest-path", isolated / "Cargo.toml"],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("copy", [False, True])
def test_a_dependency_missing_from_the_workspace_is_rejected(workspace, copy):
    crate = Crate(workspace / "foo-sys" / "Cargo.toml", template="lib")
    before = crate.cargo_toml.read_text()

    with pytest.raises(ValueError, match="not in \\[workspace.dependencies\\]"):
        crate.cargo_add("cfg-if", copy_from=workspace / "Cargo.toml" if copy else None)
    assert crate.cargo_toml.read_text() == before


def _port_tests(sys_crate: Crate, rs_crate: Crate) -> None:
    tests = sys_crate.cargo_toml.parent / "tests" / "io.rs"
    tests.parent.mkdir()
    tests.write_text(f"use {sys_crate.lib_name}::*;\n")
    TranslationWorkspace(sys_crate, rs_crate)._port_tests(rs_crate, tests)


def test_ported_tests_inherit_workspace_dependencies(workspace):
    sys_crate = Crate(workspace / "foo-sys" / "Cargo.toml", template="lib")
    sys_crate.cargo_add("libc", section="dev", copy_from=workspace / "Cargo.toml")
    rs_crate = Crate(workspace / "foo-rs" / "Cargo.toml", template="lib")

    _port_tests(sys_crate, rs_crate)

    assert _section(rs_crate, "dev-dependencies") == {
        "libc": {"workspace": True, "features": ["extra_traits"]}
    }


def test_binary_shipped_tests_remain_available_for_refactoring(workspace):
    sys_crate = Crate(workspace / "foo-sys" / "Cargo.toml", template="lib", vcs="none")
    rs_crate = Crate(workspace / "foo-rs" / "Cargo.toml", template="lib", vcs="none")
    tests = sys_crate.cargo_toml.parent / "tests" / "io.rs"
    tests.parent.mkdir()
    tests.write_text(f"use {sys_crate.lib_name}::*;\n")
    translation = TranslationWorkspace(sys_crate, rs_crate, tests=tests)
    harness = rs_crate.cargo_toml.parent / "tests" / "io.rs"

    with translation.shipped_tests():
        assert harness.exists()

    assert harness.exists()
    assert _section(rs_crate, "dev-dependencies") == {"foo-sys": {"workspace": True}}


def test_porting_a_dependency_missing_from_the_workspace_is_rejected(workspace):
    sys_crate = Crate(workspace / "foo-sys" / "Cargo.toml", template="lib")
    with sys_crate.cargo_toml.open("a") as manifest:
        manifest.write('\n[dev-dependencies]\ncfg-if = "1"\n')
    rs_crate = Crate(workspace / "foo-rs" / "Cargo.toml", template="lib")

    with pytest.raises(ValueError, match="`cfg-if` is not in \\[workspace.dependencies\\]"):
        _port_tests(sys_crate, rs_crate)


def _safe_crate_dependent(workspace: Path) -> tuple[Crate, Crate]:
    rs_crate = Crate(workspace / "libfoo-rs" / "Cargo.toml", template="lib")
    assert rs_crate.lib_src_path is not None
    rs_crate.lib_src_path.write_text("pub fn bar() -> i32 { 1 }\n")
    hybrid_crate = Crate(workspace / "libfoo-hybrid" / "Cargo.toml", template="lib")
    hybrid_crate.add_workspace_dependencies([{SAFE_CRATE: rs_crate.name}])
    return rs_crate, hybrid_crate


def test_a_renamed_dependency_is_only_reachable_by_its_alias(workspace):
    rs_crate, hybrid_crate = _safe_crate_dependent(workspace)

    assert _section(hybrid_crate, "dependencies") == {
        SAFE_CRATE: {"path": "../libfoo-rs", "package": rs_crate.name}
    }
    # Other members can still inherit the package under its own name
    assert tomlkit.loads((workspace / "Cargo.toml").read_text()).unwrap()["workspace"][
        "dependencies"
    ][rs_crate.name] == {"path": "libfoo-rs"}

    assert hybrid_crate.lib_src_path is not None
    hybrid_crate.lib_src_path.write_text(f"pub fn f() -> i32 {{ {SAFE_CRATE}::bar() }}\n")
    builds, output = hybrid_crate.cargo_build()
    assert builds, output


@pytest.mark.parametrize(
    "code, error",
    [
        ("pub fn f() -> i32 { libfoo_rs::bar() }", "E0433"),
        ("pub use ::libfoo_rs::bar;", "E0432"),
        ("extern crate libfoo_rs;", "E0463"),
    ],
)
def test_rustc_rejects_the_renamed_dependency_by_its_own_name(workspace, code, error):
    _, hybrid_crate = _safe_crate_dependent(workspace)

    assert hybrid_crate.lib_src_path is not None
    hybrid_crate.lib_src_path.write_text(f"{code}\n")
    builds, output = hybrid_crate.cargo_build()
    assert not builds
    assert f"error[{error}]" in output


def test_plain_and_aliased_dependencies_mix_in_one_call(workspace):
    Crate(workspace / "libfoo-rs" / "Cargo.toml", template="lib")
    Crate(workspace / "libfoo-sys" / "Cargo.toml", template="lib")
    hybrid_crate = Crate(workspace / "libfoo-hybrid" / "Cargo.toml", template="lib")

    hybrid_crate.add_workspace_dependencies([{SAFE_CRATE: "libfoo-rs"}, "libfoo-sys"])

    assert _section(hybrid_crate, "dependencies") == {
        SAFE_CRATE: {"path": "../libfoo-rs", "package": "libfoo-rs"},
        "libfoo-sys": {"workspace": True},
    }


def test_an_empty_alias_is_rejected(workspace):
    Crate(workspace / "libfoo-rs" / "Cargo.toml", template="lib")
    crate = Crate(workspace / "libfoo-hybrid" / "Cargo.toml", template="lib")
    before = crate.cargo_toml.read_text()

    with pytest.raises(ValueError, match="must be non-empty"):
        crate.add_workspace_dependencies([{"": "libfoo-rs"}])
    assert crate.cargo_toml.read_text() == before
