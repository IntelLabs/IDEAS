#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
import os
import json
import logging
import shutil

from dataclasses import dataclass
from pathlib import Path

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore

from .tools import run_subprocess, LARGE_PROJECT

logger = logging.getLogger("ideas.cmake")


@dataclass
class CmakeConfig:
    source_dir: Path = MISSING
    build_dir: Path = MISSING


cs = ConfigStore.instance()
cs.store(name="cmake", node=CmakeConfig)


def _normalize_isystem(compile_commands_path: Path) -> None:
    """Replace -isystem with -I in compile_commands.json"""
    if not compile_commands_path.exists():
        return
    db = json.loads(compile_commands_path.read_text())
    for entry in db:
        if "command" in entry:
            entry["command"] = entry["command"].replace("-isystem", "-I")

        if "arguments" in entry:
            entry["arguments"] = [
                "-I" + arg[len("-isystem") :] if arg.startswith("-isystem") else arg
                for arg in entry["arguments"]
            ]
    compile_commands_path.write_text(json.dumps(db, indent=2))


def configure(
    source_dir: Path,
    build_dir: Path,
    preset: str | None = None,
) -> None:
    # Clean existing build directory
    shutil.rmtree(build_dir, ignore_errors=True)

    flags = [
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
        "-DCMAKE_C_COMPILER=clang",
    ]
    if extract_info_cmake := os.environ.get("EXTRACT_INFO_CMAKE"):
        flags.append(f"-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES={extract_info_cmake}")
    if cflags := os.environ.get("CFLAGS"):
        flags.append(f"-DCMAKE_C_FLAGS={cflags}")

    if not preset:
        cmd = ["cmake", "-S", str(source_dir), "-B", str(build_dir), "-G", "Ninja"] + flags
    else:
        cmd = ["cmake", "-S", ".", "--preset", preset] + flags

    success, output, error, _ = run_subprocess(cmd)
    if not success:
        raise RuntimeError(f"CMake configuration failed:{' '.join(cmd)}\n{output + error}")

    # Replace -isystem with -I in compile_commands.json so that all project
    # headers get consistent USRs regardless of CMake SYSTEM keyword usage.
    if LARGE_PROJECT:
        _normalize_isystem(build_dir / "compile_commands.json")


def build(build_dir: Path, preset: str | None = None) -> None:
    if not preset:
        cmd = ["cmake", "--build", str(build_dir), "--target", "all"]
    else:
        cmd = ["cmake", "--build", str(build_dir), "--target", "all", "--preset", preset]

    build_log_path = build_dir / "build.log"
    success, output, error, _ = run_subprocess(cmd)
    if not success:
        with open(build_log_path, "w") as log_file:
            log_file.write(output + error)
        raise RuntimeError(f"CMake build failed: {' '.join(cmd)}\n{output + error}")


def _main(cfg: CmakeConfig) -> None:
    # Determine Cmake preset
    preset = "test" if os.path.exists("CMakePresets.json") else None

    # Configure Cmake
    configure(
        source_dir=cfg.source_dir,
        build_dir=cfg.build_dir,
        preset=preset,
    )

    # Build with Cmake
    build(cfg.build_dir, preset)


@hydra.main(version_base=None, config_name="cmake")
def main(cfg: CmakeConfig) -> None:
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
