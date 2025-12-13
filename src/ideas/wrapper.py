#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import io
import re
import logging
from pathlib import Path
from contextlib import redirect_stdout
from dataclasses import dataclass, field

import dspy
import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas import model, ModelConfig, GenerateConfig, tools
from ideas.tools import Crate, get_info_from_cargo_toml

logger = logging.getLogger("ideas.wrapper")


@dataclass
class WrapperConfig:
    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)

    symbols: Path = MISSING
    cargo_toml: Path = MISSING

    max_iters: int = 5


cs = ConfigStore.instance()
cs.store(name="wrapper", node=WrapperConfig)


class WrapperGenerator(dspy.Module):
    class Signature(dspy.Signature):
        """
        Implement a C-compatible FFI wrapper for `crate::{symbol_name}` by replacing the `unimplemented!()` macro in `example_wrapper`.
        **Only** modify the body of the function, and **nothing** else.
        The implementation for `crate::{symbol_name}` is in a crate that was read from "{crate_path}".
        Assume the types in `crate::wrapper::` do not have the same memory layout as those in `crate::`.
        The wrapper should properly convert between `crate::wrapper::` and `crate::` types by copying the values from the wrapper type to the crate type before calling `crate::{symbol_name}`.
        After this conversion, the wrapper should call the Rust function `crate::{symbol_name}`.
        After the call to `crate::{symbol_name}`, the wrapper should convert back the `crate::` types to `crate::wrapper::` types.
        The wrapper will be written to "{wrapper_path}".
        Use the feedback, if provided, from `cargo build` about the `prior_wrapper` when generating the wrapper.
        """

        crate: dspy.Code["Rust"] = dspy.InputField()  # noqa: F821
        example_wrapper: dspy.Code["Rust"] = dspy.InputField()  # noqa: F821
        wrapper: dspy.Code["Rust"] = dspy.OutputField()  # noqa: F821
        prior_wrapper: dspy.Code["Rust"] = dspy.InputField()  # noqa: F821
        feedback: str = dspy.InputField()

    def __init__(
        self,
        max_iters: int,
    ) -> None:
        super().__init__()

        self.max_iters = max_iters

    def forward(self, symbol_name: str, crate: Crate) -> dspy.Prediction:
        crate_path = crate.rust_src_path
        wrapper_path = crate.rust_src_path.parent / "wrapper.rs"
        symbol_wrapper_path = crate.rust_src_path.parent / "wrapper" / f"{symbol_name}.rs"
        example_wrapper = symbol_wrapper_path.read_text().strip()

        # Replace lines containing unimplemented!() with anything
        allowed_changes = re.escape(example_wrapper)
        allowed_changes = re.sub(
            r"^[\ \\t]*unimplemented!\\\(\\\);[\ \\t]*$",
            r".*",
            allowed_changes,
            flags=re.MULTILINE,
        )

        signature = WrapperGenerator.Signature.with_instructions(
            WrapperGenerator.Signature.instructions.format(
                symbol_name=symbol_name,
                crate_path="src/lib.rs",
                wrapper_path=f"src/wrapper/{symbol_name}.rs",
            )
        )
        generate_wrapper = dspy.ChainOfThought(signature)

        orig_wrapper = wrapper_path.read_text()
        orig_code = crate_path.read_text()

        # Add "pub mod wrapper;" to code
        code = orig_code
        if not re.search(r"^pub mod wrapper;$", code, flags=re.MULTILINE):
            code = f"pub mod wrapper;\n\n{code}"
            crate_path.write_text(code)

        # Add "pub mod {symbol_name};" to wrapper
        if not re.search(
            rf"^pub mod {re.escape(symbol_name)};$", orig_wrapper, flags=re.MULTILINE
        ):
            with wrapper_path.open("a+") as f:
                f.write(f"pub mod {symbol_name};\n")

        i, wrapper, success, feedback, prior_wrapper = 0, "", False, "", example_wrapper
        for i in range(self.max_iters):
            pred = generate_wrapper(
                crate=code,
                example_wrapper=example_wrapper,
                feedback=feedback,
                prior_wrapper=prior_wrapper,
            )
            if pred.wrapper is None:
                feedback = "No wrapper was generated. You must respect the template and instructions **exactly**!"
                continue
            prior_wrapper = wrapper = pred.wrapper.code

            # Enforce only function body changes
            matches = re.match(f"^{allowed_changes}$", wrapper, flags=re.DOTALL)
            if matches is None:
                feedback = (
                    "The generated wrapper modifies parts outside the function body."
                    "You must **only** modify the `unimplemented!()` function body and leave everything else **unchanged**!"
                )
                continue

            # Write wrapper to disk and check if we build
            symbol_wrapper_path.write_text(wrapper)
            success, feedback = tools.run_subprocess(
                [
                    "cargo",
                    "build",
                    "--quiet",
                    "--color=never",
                    f"--manifest-path={crate.cargo_toml}",
                ]
            )
            if success:
                break

        # Write original code and wrapper and return wrapper
        crate_path.write_text(orig_code)
        wrapper_path.write_text(orig_wrapper)
        return dspy.Prediction(
            wrapper=wrapper, example_wrapper=example_wrapper, success=success, iters=i
        )

    def get_history(self, n: int = 1, clear: bool = False) -> str:
        f = io.StringIO()
        with redirect_stdout(f):
            self.inspect_history(n=n, clear=clear)
        return f.getvalue().strip()

    def inspect_history(self, n: int = 1, clear: bool = True):
        super().inspect_history(n)
        if clear:
            self.history = []


@hydra.main(version_base=None, config_name="wrapper")
def main(cfg: WrapperConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger.info(f"Saving results to {output_dir}")

    model.configure(cfg.model, cfg.generate)

    # Get crate info and update src to include reference to wrapper
    crate = get_info_from_cargo_toml(cfg.cargo_toml)
    if not re.search(
        r"^pub mod wrapper;$", crate.rust_src_path.read_text(), flags=re.MULTILINE
    ):
        with crate.rust_src_path.open("a+") as f:
            f.write("\n\npub mod wrapper;\n")
    wrapper_path = crate.rust_src_path.parent / "wrapper.rs"
    wrapper_path.write_text("")

    # Generate wrappers for each symbol
    for symbol_name in cfg.symbols.read_text().splitlines():
        logger.info(f"Generating wrapper for `{symbol_name}` ...")
        agent = WrapperGenerator(max_iters=cfg.max_iters)
        pred = agent(symbol_name, crate)

        # Write wrapper to disk and reference in wrapper.rs
        symbol_wrapper = pred.wrapper
        if not pred.success:
            symbol_wrapper = pred.example_wrapper
            # Write the failure example wrapper for debugging
            failed_wrapper_path = (
                crate.rust_src_path.parent / "wrapper" / f"{symbol_name}.rs.failure"
            )
            failed_wrapper_path.parent.mkdir(exist_ok=True, parents=True)
            failed_wrapper_path.write_text(pred.wrapper)

        symbol_wrapper_path = crate.rust_src_path.parent / "wrapper" / f"{symbol_name}.rs"
        symbol_wrapper_path.parent.mkdir(exist_ok=True, parents=True)
        symbol_wrapper_path.write_text(symbol_wrapper)
        symbol_wrapper_path.with_suffix(".history").write_text(agent.get_history(n=100000))

        with wrapper_path.open("a+") as f:
            f.write(f"pub mod {symbol_name};\n")


if __name__ == "__main__":
    main()
