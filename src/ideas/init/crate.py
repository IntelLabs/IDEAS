#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
import logging
from pathlib import Path
from dataclasses import dataclass

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas.tools import Crate

logger = logging.getLogger("ideas.init.crate")


@dataclass
class CrateConfig:
    crate_type: str = MISSING
    vcs: str = "none"

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

    # Add static dependencies and sections
    crate.cargo_add(dep="openssl@0.10.75")
    crate.cargo_add(dep="cc@1.2.53", section="build")
    if cfg.crate_type == "lib":
        with crate.cargo_toml.open("a") as f:
            f.write('\n[lib]\ncrate-type = ["lib", "cdylib"]\n')
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
