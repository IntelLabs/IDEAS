#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


import sys
import logging

from textwrap import dedent as d
from dataclasses import dataclass
from pathlib import Path

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore

from ideas.tools import Workspace

logger = logging.getLogger("ideas.init.workspace")


@dataclass
class WorkspaceConfig:
    cargo_toml: Path = MISSING
    vcs: str = "none"

    def __post_init__(self):
        if self.vcs not in ["git", "none"]:
            raise ValueError(f"Invalid VCS: {self.vcs}!")


cs = ConfigStore.instance()
cs.store(name="init.workspace", node=WorkspaceConfig)


def _main(cfg: WorkspaceConfig) -> None:
    # Initialize workspace
    workspace = Workspace(cfg.cargo_toml, vcs=cfg.vcs)  # type: ignore[reportArgumentType]

    if cfg.vcs == "git":
        # Write .gitignore
        (cfg.cargo_toml.parent / ".gitignore").write_text(
            d("""
            Cargo.lock
            target/
            *.log
            *.jsonl
            """).strip()
        )

    # Commit initial repo
    workspace.vcs.add(Path("Cargo.toml"))
    workspace.vcs.add(Path(".gitignore"))
    msg = "Created cargo workspace"
    logger.info(msg)
    workspace.vcs.commit(msg)


@hydra.main(version_base=None, config_name="init.workspace")
def main(cfg: WorkspaceConfig) -> None:
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
