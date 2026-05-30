#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
import logging

import dspy

from ideas.tools import Crate
from ideas.ast import Symbol
from ideas.init.build import write_main_binding

logger = logging.getLogger("ideas.test_symbol")


class SymbolTester(dspy.Module):
    def __init__(self, crate: Crate, symbols: list[Symbol], tests: str):
        super().__init__()
        self.crate = crate
        self.tests = tests

        for symbol in symbols:
            if not (symbol.is_function and symbol.is_definition and symbol.is_global):
                continue
            if self.crate.is_bin and symbol.spelling == "main":
                # main requires special handling because we must bind to it as _main and
                # statically create a Rust main that calls it
                self.main_function: str = write_main_binding(crate)

    def test(
        self, tests: str, skip: list[str] | None = None
    ) -> tuple[bool, dict[str, bool], str]:
        rust_src = self.crate.rust_src_path.read_text()

        # Remove forbid unsafe from Rust source
        rust_src = rust_src.replace("#![forbid(unsafe_code)]", "")

        # Reference wrapper module in Rust source
        WRAPPER_MOD = "pub mod wrapper;"
        if WRAPPER_MOD not in rust_src:
            rust_src += WRAPPER_MOD + "\n"
        wrapper_path = self.crate.rust_src_path.parent / "wrapper.rs"
        wrapper_path.touch()

        # Reference binding module in Rust source
        BINDING_MOD = "pub mod binding;"
        if BINDING_MOD not in rust_src:
            rust_src += BINDING_MOD + "\n"
        binding_path = self.crate.rust_src_path.parent / "binding.rs"
        binding_path.touch()

        self.crate.rust_src_path.write_text(rust_src)

        # Try building the crate to detect if we need to insert a main
        builds, feedback = self.crate.cargo_build(fix_E0601=False)
        if "error[E0601]" in feedback and self.main_function:
            with self.crate.rust_src_path.open("a+") as f:
                f.write(self.main_function)

        self.crate.vcs.add(wrapper_path, binding_path, self.crate.rust_src_path)

        # Make sure the crate builds before testing
        builds, feedback = self.crate.cargo_build(fix_E0601=False)
        if not builds:
            raise RuntimeError(f"Crate does not build!\n{feedback}")

        passes, jsonl, error, _ = self.crate.cargo_test(
            tests, skip=skip, test_harness="nextest run", message_format="libtest-json"
        )
        results = extract_test_results(jsonl)
        return passes, results, error

    def forward(self, symbol: Symbol, skip: list[str] | None = None) -> dspy.Prediction:
        logger.info(f"Testing symbol `{symbol.name}` ....")

        # These files are modified by test
        binding_path = self.crate.rust_src_path.parent / "binding.rs"
        orig_binding_src = binding_path.read_bytes()
        orig_rust_src = self.crate.rust_src_path.read_bytes()

        # Run cargo test
        passes, results, feedback = self.test(self.tests, skip=skip)
        if passes:
            msg = f"Tested symbol `{symbol.name}`"
            logger.info(msg)
        else:
            feedback = "Running `cargo test` fails!\n" + feedback
            msg = f"Failed to test symbol `{symbol.name}`"
            logger.error(msg)
            msg += f"\n\n{feedback}"
        self.crate.vcs.commit(msg)

        # Restore originals
        binding_path.write_bytes(orig_binding_src)
        self.crate.rust_src_path.write_bytes(orig_rust_src)

        pred = dspy.Prediction(
            success=passes,
            output=feedback,
            results=results,
            feedback="",
        )

        if not passes:
            # FIXME: Use test feedback?
            pred.feedback = (
                "The current Rust translation in `prior_translation` does not match the behavior of the C `snippet`. "
                "Carefully compare `prior_translation` against the C `snippet` and regenerate the Rust `translation` to match the C behavior exactly. "
                "Do not assume inputs are well-formed: if the tests exercise malformed, invalid, partial, or adversarial input, preserve the C behavior for those cases too, including error returns, boundary handling, or other observable effects. "
                "Make minimal, targeted changes to `prior_translation`, and only modify what is necessary to match the C behavior. "
                "Treat the C `snippet` as the source of truth, even if it contains a bug."
            )

        return pred


def extract_test_results(output: str) -> dict[str, bool]:
    test_results: dict[str, bool] = {}

    for line in output.splitlines():
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "test":
            continue
        event = obj.get("event")
        if event not in {"ok", "failed", "ignored"}:
            continue
        name = str(obj.get("name", "")).rsplit("$", 1)[-1].strip()
        if name:
            # Treat ignored as non-failing for disable-list purposes
            test_results[name] = event != "failed"

    return test_results
