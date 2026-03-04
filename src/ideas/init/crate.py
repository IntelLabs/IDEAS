#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from pathlib import Path
from dataclasses import dataclass

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas.tools import Crate

logger = logging.getLogger("ideas.preprocess")


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


@hydra.main(version_base=None, config_name="init.crate")
def main(cfg: CrateConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)

    # Initialize crate
    crate = Crate(
        cargo_toml=output_dir / "Cargo.toml",
        type=cfg.crate_type,  # type: ignore[reportArgumentType]
        vcs=cfg.vcs,  # type: ignore[reportArgumentType]
    )
    crate.add(crate.cargo_toml)

    # Add static dependencies and sections
    crate.cargo_add(dep="openssl@0.10.75")
    if cfg.crate_type == "lib":
        with crate.cargo_toml.open("a") as f:
            f.write('\n[lib]\ncrate-type = ["lib", "cdylib"]\n')
        crate.invalidate_metadata()

    # Add hydra directory
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.add(output_dir / output_subdir)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        logger.error(e)
        raise e
