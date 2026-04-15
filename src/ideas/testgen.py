#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
import json
import logging
from pathlib import Path
from dataclasses import dataclass

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas.tools import run_subprocess


logger = logging.getLogger("ideas.testgen")


@dataclass
class TestgenConfig:
    artifact: Path = MISSING
    test_vector: Path = MISSING


cs = ConfigStore.instance()
cs.store(name="testgen", node=TestgenConfig)


def _main(cfg: TestgenConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger.info(f"Saving results to {output_dir}")

    # Run the artifact and collect outputs
    success, output, error, returncode = run_subprocess([str(cfg.artifact)])

    # Stop the app on timeout
    if returncode == "timeout":
        raise RuntimeError(
            f"Artifact {cfg.artifact} timed out! This may be due to indefinite waiting for `stdin`!"
        )

    if not success:
        logger.warning(
            f"Artifact {cfg.artifact} failed execution with return code {returncode}! The test vector will expect an error."
        )

    # Write the .json test_vector
    test_vector = {
        "stdout": {"pattern": f"{output}"},
        "stderr": {"pattern": f"{error}"},
        "rc": returncode,
    }
    cfg.test_vector.parent.mkdir(parents=True, exist_ok=True)
    with open(cfg.test_vector, "w") as f:
        json.dump(test_vector, f, indent=2)


@hydra.main(version_base=None, config_name="testgen")
def main(cfg: TestgenConfig) -> None:
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
