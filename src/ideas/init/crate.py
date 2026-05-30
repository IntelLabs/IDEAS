#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
import logging
import tomlkit
from pathlib import Path
from dataclasses import dataclass

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas.tools import Crate, LARGE_PROJECT

logger = logging.getLogger("ideas.init.crate")


@dataclass
class CrateConfig:
    crate_type: str = MISSING
    vcs: str = "none"

    reexport_lib: bool = True

    def __post_init__(self):
        if self.crate_type not in ["bin", "lib"]:
            raise ValueError(f"Invalid crate type: {self.crate_type}!")
        if self.vcs not in ["git", "none"]:
            raise ValueError(f"Invalid VCS: {self.vcs}!")


cs = ConfigStore.instance()
cs.store(name="init.crate", node=CrateConfig)


def _main(cfg: CrateConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)

    # Initialize crate
    crate = Crate(
        cargo_toml=output_dir / "Cargo.toml",
        type=cfg.crate_type,  # type: ignore[reportArgumentType]
        vcs=cfg.vcs,  # type: ignore[reportArgumentType]
    )

    # Delete default cargo init code
    crate.rust_src_path.write_text("")

    # Add static dependencies
    crate.cargo_add(dep="libc@0.2.185")
    crate.cargo_add(dep="openssl@0.10.79")
    if LARGE_PROJECT:
        crate.cargo_add(dep="flate2@1")
        crate.cargo_add(dep="regex@1")
    crate.cargo_add(dep="serde@1", section="dev", features=["derive"])
    crate.cargo_add(dep="serde_json@1", section="dev")
    crate.cargo_add(dep="tempfile@3", section="dev")
    crate.cargo_add(dep="cc@1.2.53", section="build")

    if cfg.crate_type == "bin":
        # Add static test dependencies
        crate.cargo_add(dep="assert_cmd@2.0.17", section="dev")
        crate.cargo_add(dep="predicates@3.1.3", section="dev")

    # Disable default tests
    cargo_toml = tomlkit.loads(crate.cargo_toml.read_text())
    if cfg.crate_type == "bin":
        bin, found = cargo_toml.get("bin", list()), False
        for target in bin:
            if target.get("name", None) == crate.root_package["name"]:
                target["test"], found = False, True
                break
        if not found:
            bin.append({"name": crate.root_package["name"], "test": False})
        cargo_toml["bin"] = bin
    if cfg.crate_type == "lib":
        lib = cargo_toml.get("lib", dict())
        lib.update({"test": False, "doctest": False})
        cargo_toml["lib"] = lib
    crate.cargo_toml.write_text(tomlkit.dumps(cargo_toml))

    # Export cdylib
    if cfg.crate_type == "lib" and cfg.reexport_lib:
        cargo_toml = tomlkit.loads(crate.cargo_toml.read_text())
        lib = cargo_toml.get("lib", dict())
        lib.update({"crate-type": ["lib", "cdylib"]})
        cargo_toml["lib"] = lib
        crate.cargo_toml.write_text(tomlkit.dumps(cargo_toml))

    # Disable lints
    cargo_toml = tomlkit.loads(crate.cargo_toml.read_text())
    lints = tomlkit.table(is_super_table=True)
    lints.add("rust", {"nonstandard_style": "allow"})
    cargo_toml["lints"] = lints
    crate.cargo_toml.write_text(tomlkit.dumps(cargo_toml))

    # Configure testing
    crate.cargo_nextest_config()
    crate.invalidate_metadata()

    # Add cargo, workspace cargo, hydra log directory to VCS
    crate.vcs.add(crate.cargo_toml, crate.rust_src_path)
    if crate.metadata.get("workspace_root", None):
        crate.vcs.add(Path(crate.metadata["workspace_root"]) / "Cargo.toml")
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.vcs.add(output_dir / output_subdir)
    msg = f"Initialized crate `{crate.root_package['name']}`"
    logger.info(msg)
    crate.vcs.commit(msg)


@hydra.main(version_base=None, config_name="init.crate")
def main(cfg: CrateConfig) -> None:
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
