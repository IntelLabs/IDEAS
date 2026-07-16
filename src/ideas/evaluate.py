#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


import re
import sys
import logging
from dataclasses import dataclass
from pathlib import Path

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore

from ideas.tools import Crate, nextest_json_to_libtest


logger = logging.getLogger("ideas.evaluate")


@dataclass
class EvaluateConfig:
    manifest: Path = MISSING
    test_cases: str = MISSING

    output_file: Path = MISSING


cs = ConfigStore.instance()
cs.store(name="evaluate", node=EvaluateConfig)


_TEST_FN_RE = re.compile(r"^\s*fn\s+(\w+)\s*\(", re.MULTILINE)


def list_tests(test_file: Path) -> list[str]:
    """Parse #[test] function names directly from a .rs integration test file."""
    source = test_file.read_text()
    tests = []
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == "#[test]":
            for subsequent in lines[i + 1 :]:
                m = _TEST_FN_RE.match(subsequent)
                if m:
                    tests.append(m.group(1))
                    break
                # Skip attributes/comments between #[test] and fn
                if subsequent.strip() and not subsequent.strip().startswith(("#", "/")):
                    break
    return tests


def _main(cfg: EvaluateConfig) -> None:
    # Resolve integration test file (error loudly if missing)
    crate = Crate(cfg.manifest, vcs="none")
    test_file = crate.cargo_toml.parent / "tests" / f"{cfg.test_cases}.rs"
    if not test_file.exists():
        raise FileNotFoundError(f"Integration test file not found: {test_file}")

    # Attempt to build the evaluation test
    builds, _, _, _ = crate.cargo_test(
        name=cfg.test_cases, quiet=False, fail_fast=True, build_only=True
    )
    if builds:
        # Use libtest-json output, parse it, and reformat for readability
        # stderr contains the native nextest output
        _, stdout, stderr, _ = crate.cargo_test(
            name=cfg.test_cases, message_format="libtest-json"
        )
        output = nextest_json_to_libtest(stdout) + stderr
    else:
        output = f"Failed to build test target {cfg.test_cases} for evaluation!\n"
        names = list_tests(test_file)
        lines = [f"test {name} ... FAILED" for name in names]
        output += "\n".join(lines)
        if lines:
            output += "\n"

    # Write to output file
    cfg.output_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.output_file.write_text(output)
    crate.vcs.add(cfg.output_file)
    crate.vcs.commit(f"Evaluation results for {cfg.test_cases}")
    print(output)


@hydra.main(version_base=None, config_name="evaluate")
def main(cfg: EvaluateConfig) -> None:
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
