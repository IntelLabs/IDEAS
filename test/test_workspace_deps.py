#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import tomlkit

from ideas.hybrid import SAFE_CRATE
from ideas.tools import Crate
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
    TranslationWorkspace._port_tests(SimpleNamespace(sys_crate=sys_crate), rs_crate, tests)  # type: ignore[arg-type]


def test_ported_tests_inherit_workspace_dependencies(workspace):
    sys_crate = Crate(workspace / "foo-sys" / "Cargo.toml", template="lib")
    sys_crate.cargo_add("libc", section="dev", copy_from=workspace / "Cargo.toml")
    rs_crate = Crate(workspace / "foo-rs" / "Cargo.toml", template="lib")

    _port_tests(sys_crate, rs_crate)

    assert _section(rs_crate, "dev-dependencies") == {
        "libc": {"workspace": True, "features": ["extra_traits"]}
    }


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
