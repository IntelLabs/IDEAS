#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import logging
import shutil

from dataclasses import dataclass
from pathlib import Path

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore

from .tools import run_subprocess

logger = logging.getLogger("ideas.cmake")


@dataclass
class CmakeConfig:
    source_dir: Path = MISSING
    build_dir: Path = MISSING


cs = ConfigStore.instance()
cs.store(name="cmake", node=CmakeConfig)


def configure(
    source_dir: Path,
    build_dir: Path,
    preset: str | None = None,
) -> None:
    # Clean existing build directory
    shutil.rmtree(build_dir, ignore_errors=True)

    flags = [
        "-DCMAKE_BUILD_TYPE=Debug",
        "-DCMAKE_C_FLAGS_DEBUG=-g -O0",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    ]
    if extract_info_cmake := os.environ.get("EXTRACT_INFO_CMAKE"):
        flags.append(f"-DCMAKE_PROJECT_TOP_LEVEL_INCLUDES={extract_info_cmake}")
    if cflags := os.environ.get("CFLAGS"):
        flags.append(f"-DCMAKE_C_FLAGS={cflags}")

    if not preset:
        cmd = ["cmake", "-S", str(source_dir), "-B", str(build_dir), "-G", "Ninja"] + flags
    else:
        cmd = ["cmake", "-S", ".", "--preset", preset] + flags

    success, output = run_subprocess(cmd)
    if not success:
        raise RuntimeError(f"CMake configuration failed!\n{output}")


def build(build_dir: Path, preset: str | None = None) -> None:
    if not preset:
        cmd = ["cmake", "--build", str(build_dir), "--target", "all"]
    else:
        cmd = ["cmake", "--build", str(build_dir), "--target", "all", "--preset", preset]

    build_log_path = build_dir / "build.log"
    success, output = run_subprocess(cmd)
    if not success:
        with open(build_log_path, "w") as log_file:
            log_file.write(output)
        raise RuntimeError(f"CMake build failed!\n{output}")


def extract_symbols(build_dir: Path) -> None:
    # Find executables
    cmd = ["find", str(build_dir), "-maxdepth", "1", "-type", "f", "-executable"]
    success, output = run_subprocess(cmd)
    if not success:
        raise RuntimeError(f"Finding executables failed!\n{output}")

    executables = output.strip().split("\n")
    for exe in executables:
        if not exe:
            raise RuntimeError(f"Found an empty line in the list of executables {executables}!")

        # Extract symbols using nm and awk
        cmd = ["nm", "--extern-only", exe]
        success, output = run_subprocess(cmd)
        if not success:
            raise RuntimeError(f"Extracting symbols from {exe} failed!\n{output}")

        # Filter for text symbols (T) and exclude symbols starting with _
        symbols = []
        for line in output.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "T":
                symbol = parts[-1]
                if not symbol.startswith("_"):
                    symbols.append(symbol)

        # Write symbols to file
        symbol_file = f"{exe}.symbols"
        with open(symbol_file, "w") as f:
            f.write("\n".join(symbols) + "\n")


@hydra.main(version_base=None, config_name="cmake")
def main(cfg: CmakeConfig) -> None:
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

    # Extract per-target symbols
    extract_symbols(build_dir=cfg.build_dir)


if __name__ == "__main__":
    main()
