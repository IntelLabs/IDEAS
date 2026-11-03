#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
import json
import logging
from typing import cast
from pathlib import Path
from dataclasses import dataclass

import hydra
from omegaconf import MISSING, OmegaConf
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from .oracle import extract_test_events
from .tools import Crate

logger = logging.getLogger("ideas.minify")


@dataclass
class MinifyConfig:
    cargo_toml: Path = MISSING
    tests: str | None = MISSING
    vcs: str = "none"


cs = ConfigStore.instance()
cs.store(name="minify", node=MinifyConfig)


def _restore(root: Path, snapshot: dict[Path, bytes]) -> None:
    current = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    for path in current.keys() - snapshot.keys():
        path.unlink()
    for path, content in snapshot.items():
        if current.get(path) != content:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


def minify(crate: Crate, tests: str) -> tuple[list[Path], str]:
    # Returns the files it minified and a status message
    _, output, _, _ = crate.cargo_test(tests, message_format="libtest-json")
    before = extract_test_events(output)
    if "ok" not in before.values():
        raise RuntimeError(
            f"No minifying `{crate.name}`: no test in `{tests}` passes to verify it"
        )

    sources = crate.cargo_toml.parent / "src"
    snapshot = {path: path.read_bytes() for path in sources.rglob("*") if path.is_file()}
    verified = False
    try:
        minified, output = crate.cargo_minify()
        if not minified:
            raise RuntimeError(f"No minifying `{crate.name}`: `cargo minify` fails!\n{output}")

        current = {path: path.read_bytes() for path in sources.rglob("*") if path.is_file()}
        changed = sorted(
            p for p in snapshot.keys() | current.keys() if snapshot.get(p) != current.get(p)
        )
        if not changed:
            msg = f"No minifying `{crate.name}`: nothing to minify"
            logger.info(msg)
            return [], msg

        # We expect exactly the same outcomes as a proxy for non-material changes
        # TODO: Make this more robust
        _, output, _, _ = crate.cargo_test(tests, message_format="libtest-json")
        after = extract_test_events(output)
        if diverged := sorted(
            t for t in before.keys() | after.keys() if before.get(t) != after.get(t)
        ):
            outcomes = "\n".join(
                f"- `{t}`: {before.get(t, 'missing')} -> {after.get(t, 'missing')}"
                for t in diverged
            )
            raise RuntimeError(
                f"No minifying `{crate.name}`: test outcome(s) in `{tests}` changed\n{outcomes}"
            )

        verified = True
        passing = sum(event == "ok" for event in after.values())
        msg = f"Minified `{crate.name}`: the same {passing}/{len(after)} test(s) in `{tests}` pass"
        logger.info(msg)
        return changed, msg
    finally:
        # Fallback on everything
        if not verified:
            _restore(sources, snapshot)


def _lines(sources: Path) -> int:
    return sum(
        len(path.read_bytes().splitlines()) for path in sources.rglob("*") if path.is_file()
    )


def _record(path: Path, lines_before: int, lines_after: int) -> None:
    stats = {
        "lines_before": lines_before,
        "lines_after": lines_after,
        "lines_deleted": lines_before - lines_after,
    }
    path.write_text(json.dumps(stats, indent=2) + "\n")


def _main(cfg: MinifyConfig) -> None:
    if cfg.tests is None:
        logger.warning(
            f"Skipped minifying `{cfg.cargo_toml.parent.name}`: "
            "it needs tests to verify that it changes nothing"
        )
        return

    crate = Crate(cfg.cargo_toml, vcs=cfg.vcs)  # type: ignore[reportArgumentType]
    sources = crate.cargo_toml.parent / "src"
    lines_before = _lines(sources)
    try:
        changed, msg = minify(crate, cfg.tests)
    except RuntimeError as e:
        logger.warning(e)
        changed, msg = [], str(e)

    stats = crate.cargo_toml.parent / "minify.json"
    _record(stats, lines_before, _lines(sources))
    crate.vcs.add(*changed, stats)
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.vcs.add(Path(HydraConfig.get().runtime.output_dir) / output_subdir)
    # Skip empty commits, so that rerunning leaves the history as it is
    unchanged, _ = crate.vcs("diff --cached --quiet")
    if not unchanged:
        crate.vcs.commit(msg)


@hydra.main(version_base=None, config_name="minify")
def main(cfg: MinifyConfig) -> None:
    try:
        _main(cast(MinifyConfig, OmegaConf.to_object(cfg)))
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
