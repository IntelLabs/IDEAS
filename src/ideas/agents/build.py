#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import sys
import logging
import shutil
import textwrap
from pathlib import Path
from dataclasses import dataclass

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas.tools import Crate, rustfmt
from ideas.tools import run_subprocess
from ideas.ast_rust import CodeRust, mangle
from ideas import create_translation_unit, extract_info_c
from ideas.init.consolidate import get_symbols_and_dependencies

logger = logging.getLogger("ideas.init.build")


@dataclass
class BuildConfig:
    instrumentation: str = MISSING
    vcs: str = "none"

    def __post_init__(self):
        if self.vcs not in ["git", "none"]:
            raise ValueError(f"Invalid VCS: {self.vcs}!")

        if self.instrumentation not in ["coverage", "sanitizers"]:
            raise ValueError(f"Invalid instrumentation: {self.instrumentation}!")


cs = ConfigStore.instance()
cs.store(name="init.build", node=BuildConfig)


def generate_build_script(instrumentation: str) -> tuple[str, str]:
    build_options, build_commands = "", ""
    if instrumentation == "coverage":
        # With UBSan
        build_options += '.flag("-fsanitize=undefined,nullability")'
        build_options += '.flag("-fsanitize-trap=all")'
        build_options += '.flag("-fprofile-instr-generate")'
        build_options += '.flag("-fcoverage-mapping")'

        build_commands += 'println!("cargo:rustc-link-lib=dylib=crypto");'
        build_commands += 'println!("cargo:rustc-link-lib=m");'
        build_commands += (
            'println!("cargo:rustc-link-search=/usr/lib/llvm-21/lib/clang/21/lib/linux/");'
        )
        build_commands += (
            'println!("cargo:rustc-link-lib=static=clang_rt.ubsan_standalone-x86_64");'
        )
    elif instrumentation == "sanitizers":
        # With UBSan and ASan
        build_options += '.flag("-fsanitize=address,undefined,nullability")'
        build_options += '.flag("-fsanitize-trap=all")'
        build_commands += 'println!("cargo:rustc-link-lib=dylib=crypto");'
        build_commands += 'println!("cargo:rustc-link-lib=m");'
        build_commands += (
            'println!("cargo:rustc-link-search=/usr/lib/llvm-21/lib/clang/21/lib/linux/");'
        )
        build_commands += (
            'println!("cargo:rustc-link-lib=static=clang_rt.ubsan_standalone-x86_64");'
        )
        build_commands += 'println!("cargo:rustc-link-lib=static=clang_rt.asan-x86_64");'
    elif instrumentation == "none":
        build_commands += 'println!("cargo:rustc-link-lib=dylib=crypto");'
        build_commands += 'println!("cargo:rustc-link-lib=m");'

    return build_options, build_commands


def write_build_script(crate: Crate, build_options: str = "", build_commands: str = "") -> Path:
    c_src_path = crate.c_src_path.relative_to(crate.cargo_toml.parent)
    build_rs_src = textwrap.dedent(
        f"""
        fn main() {{
            println!("cargo:rerun-if-changed={c_src_path}");

            cc::Build::new()
                .compiler("clang")
                .warnings(false)
                .file("{c_src_path}")
                {build_options}
                .compile("library");

            {build_commands}
        }}
        """
    )

    build_rs_path = crate.cargo_toml.parent / "build.rs"
    build_rs_path.write_text(build_rs_src)
    rustfmt(build_rs_path)
    return build_rs_path


def write_symbol_binding(crate: Crate, symbol_name: str):
    rust_spelling = mangle(symbol_name)
    symbol_binding = get_linked_binding(rust_spelling, crate.c_src_path)

    symbol_binding_path = crate.rust_src_path.parent / "binding" / f"{rust_spelling}.rs"
    symbol_binding_path.parent.mkdir(exist_ok=True)
    symbol_binding_path.write_text(
        "\n\n".join(
            [
                "#![allow(unused_attributes)]",
                symbol_binding.text,
            ]
        )
    )
    crate.vcs.add(symbol_binding_path)

    binding_path = crate.rust_src_path.parent / "binding.rs"
    with binding_path.open("a+") as f:
        f.write(f"pub mod {rust_spelling};\n")
    crate.vcs.add(binding_path)


def get_linked_binding(function_name: str, c_src_path: Path, *bindgen_args: str) -> CodeRust:
    # Use bindgen to generate binding to C symbol
    bindgen = [
        "bindgen",
        "--disable-header-comment",
        "--no-doc-comments",
        "--no-layout-tests",
        "--allowlist-function",
        function_name,
        str(c_src_path),
        "--",
        *bindgen_args,
    ]
    ok, binding, error, _ = run_subprocess(bindgen)
    if not ok:
        raise ValueError(f"`{' '.join(bindgen)}` failed!\n{binding + error}")

    # Remove \u{1} prefix from link_name attribute
    linked_binding = binding.replace('#[link_name = "\\u{1}', '#[link_name = "')
    return CodeRust(linked_binding)


def strip_instrumentation(crate: Crate) -> Path:
    # Remove coverage script and data files
    if (crate.cargo_toml.parent / "measure_coverage.sh").is_file():
        (crate.cargo_toml.parent / "measure_coverage.sh").unlink()
    for prof_file in crate.cargo_toml.parent.glob("**/*.profraw"):
        prof_file.unlink()
    for prof_file in crate.cargo_toml.parent.glob("**/*.profdata"):
        prof_file.unlink()
    if (crate.cargo_toml.parent / "profraw").is_dir():
        shutil.rmtree(crate.cargo_toml.parent / "profraw")
    if (crate.cargo_toml.parent / "json").is_dir():
        shutil.rmtree(crate.cargo_toml.parent / "json")

    # Rewrite `build.rs` to remove all instrumentation
    build_options, build_commands = generate_build_script("none")
    build_rs_path = write_build_script(
        crate, build_options=build_options, build_commands=build_commands
    )

    # Attempt to build the crate
    builds, feedback = crate.cargo_build()
    if not builds:
        raise RuntimeError(
            f"Crate at {crate.cargo_toml.parent} does not build without instrumentation!\n{feedback}"
        )

    return build_rs_path


def _main(cfg: BuildConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)

    # Fetch crate
    crate = Crate(
        cargo_toml=output_dir / "Cargo.toml",
        vcs=cfg.vcs,  # type: ignore[reportArgumentType]
    )

    # Get global symbol table
    tu = create_translation_unit(crate.c_src_path)
    asts = [extract_info_c(tu)]
    symbols, _ = get_symbols_and_dependencies(
        asts, external_symbol_names=["c:@F@main"] if crate.is_bin else None
    )
    global_functions = [
        s for s in symbols.values() if s.is_global and (s.is_function and s.is_definition)
    ]

    # Write build.rs file
    build_options, build_commands = generate_build_script(cfg.instrumentation)
    build_rs_path = write_build_script(
        crate, build_options=build_options, build_commands=build_commands
    )
    crate.vcs.add(build_rs_path)
    msg = f"Wrote `build.rs` at {build_rs_path}"
    logger.info(msg)
    crate.vcs.commit(msg)

    # Verify build with build.rs
    builds, feedback = crate.cargo_build()
    if not builds:
        raise RuntimeError(f"Crate at {output_dir} does not build with build.rs!\n{feedback}")

    # Write main function and binding to it
    main_function = "#![no_main]" if crate.is_bin else ""
    with crate.rust_src_path.open("a+") as f:
        f.write(main_function)
    crate.vcs.add(crate.rust_src_path)
    crate.vcs.commit("Added main function (if any) to Rust source")

    # Generate Rust bindings for public library functions
    if not crate.is_bin:
        binding_path = crate.rust_src_path.parent / "binding.rs"
        binding_path.write_text("")
        for symbol in global_functions:
            if not (symbol.is_function and symbol.is_definition and symbol.is_global):
                continue
            write_symbol_binding(crate, symbol.spelling)
        logger.info("Generated bindings for all global functions")

        # Make the bindings module visible in the crate
        rust_src = crate.rust_src_path.read_text()
        BINDING_MOD = "pub mod binding;"
        if not re.search(f"^{re.escape(BINDING_MOD)}$", rust_src, flags=re.MULTILINE):
            crate.rust_src_path.write_text("\n\n".join([rust_src, BINDING_MOD]))
            msg = f"Referenced `{BINDING_MOD}` in {crate.rust_src_path}"
            logger.info(msg)
        else:
            msg = f"Binding module `{BINDING_MOD}` was already referenced in {crate.rust_src_path}!"
            logger.warning(msg)
        crate.vcs.add(crate.rust_src_path)
        crate.vcs.commit(msg)

    # Attempt a final build
    builds, feedback = crate.cargo_build()
    if not builds:
        raise RuntimeError(f"Crate at {output_dir} does not build with build.rs!\n{feedback}")

    # Clean on exit
    crate.cargo_clean()


@hydra.main(version_base=None, config_name="init.build")
def main(cfg: BuildConfig) -> None:
    try:
        _main(cfg)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
