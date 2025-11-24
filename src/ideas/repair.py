#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import io
import sys
import logging
import subprocess
from pathlib import Path
from dataclasses import dataclass, field

import dspy
import hydra
from omegaconf import OmegaConf, MISSING
from hydra.types import RunMode
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas import model, ModelConfig, GenerateConfig
from ideas import get_info_from_cargo_toml
from .ast_rust import ensure_no_mangle_in_module
from .tools import Crate

logger = logging.getLogger("ideas.repair")
OmegaConf.register_new_resolver(
    "dirname",
    lambda filename: filename if filename.is_dir() else filename.parent,
    use_cache=True,
    replace=True,
)


@dataclass
class RepairConfig:
    hydra: dict = field(
        default_factory=lambda: {
            "mode": RunMode.RUN,  # https://github.com/facebookresearch/hydra/issues/2262
            "run": {"dir": "${dirname:${cargo_toml}}"},
        }
    )

    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)

    cargo_toml: Path = MISSING
    max_iters: int = 100

    ensure_no_mangle: bool = True


cs = ConfigStore.instance()
cs.store(name="repair", node=RepairConfig)


@hydra.main(version_base=None, config_name="repair")
def repair(cfg: RepairConfig) -> None:
    logger.info(f"Saving results to {HydraConfig.get().runtime.output_dir}")

    model.configure(cfg.model, cfg.generate)

    agent = RepairAgent(max_iters=cfg.max_iters, ensure_no_mangle=cfg.ensure_no_mangle)
    reparation = agent(cfg.cargo_toml)

    if "history" in reparation:
        with (cfg.cargo_toml.parent / "repair_history.log").open("w") as f:
            f.write(reparation["history"])


class Reparation(dspy.Signature):
    """Repair the Rust code to pass the given tests."""

    input_code: dspy.Code["Rust"] = dspy.InputField(desc="The source code to repair")  # noqa: F821
    test_code: dspy.Code["Rust"] = dspy.InputField(desc="The test code")  # noqa: F821
    cargo_test_output: str = dspy.InputField(desc="The output from running `cargo test`")
    repaired_code: dspy.Code["Rust"] = dspy.OutputField(  # noqa: F821
        desc="The repaired code that is functionally equivalent to the original code but passes the given tests"
    )


class RepairAgent(dspy.Module):
    def __init__(self, max_iters: int = 1, ensure_no_mangle: bool = True):
        super().__init__()

        self.max_iters = max_iters
        self.repair = dspy.ChainOfThought(Reparation)
        self.ensure_no_mangle = ensure_no_mangle

    def forward(self, cargo_toml: Path) -> dict[str, str]:
        if not cargo_toml.exists():
            raise ValueError(f"{cargo_toml=} must exist!")

        # Get target source path
        crate: Crate = get_info_from_cargo_toml(cargo_toml)

        # Get test source path
        test_targets = list(
            filter(lambda t: "test" in t["kind"], crate.root_package["targets"])
        )
        if len(test_targets) != 1:
            raise ValueError(
                f"Unhandled test targets configuration in Cargo.toml: {test_targets=}"
            )
        test_src_path = Path(test_targets[0]["src_path"])
        test_name = test_targets[0]["name"]

        # Run test-repair loop
        for _ in range(self.max_iters):
            logger.info(
                f"Running: cargo test --manifest-path {str(cargo_toml)} --test {test_name}"
            )
            out = subprocess.run(
                ["cargo", "test", "--manifest-path", cargo_toml, "--test", test_name],
                text=True,
                capture_output=True,
            )
            logger.info(f"### test_output ###\n{out.stdout}")

            if out.returncode == 0:
                break

            reparation: dspy.Prediction = self.repair(
                input_code=crate.rust_src_path.read_text(),
                test_code=test_src_path.read_text(),
                cargo_test_output=out.stdout,
            )

            repaired_code = reparation["repaired_code"].code
            # Guarantee #[unsafe(no_mangle)] for all top-level symbols
            if self.ensure_no_mangle:
                repaired_code = ensure_no_mangle_in_module(repaired_code, add=True)

            crate.rust_src_path.write_text(repaired_code)

        # Save agent history to string
        history = io.StringIO()
        sys.stdout = history
        dspy.inspect_history(100)
        sys.stdout = sys.__stdout__

        return {
            "history": history.getvalue(),
        }


if __name__ == "__main__":
    repair()
