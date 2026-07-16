#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import sys
import logging
import textwrap
from pathlib import Path
from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor

import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas.ast import Symbol
from ideas.tools import Crate, run_subprocess
from ideas.ast_rust import CodeRust, mangle
from ideas import create_translation_unit, extract_info_c
from ideas.init.consolidate import get_symbols_and_dependencies

logger = logging.getLogger("ideas.init.build")


@dataclass
class BuildConfig:
    cargo_toml: Path = MISSING
    vcs: str = "none"

    def __post_init__(self):
        if self.vcs not in ["git", "none"]:
            raise ValueError(f"Invalid VCS: {self.vcs}!")


cs = ConfigStore.instance()
cs.store(name="init.build", node=BuildConfig)


def write_build_script(crate: Crate) -> Path:
    c_src_path = crate.c_src_path.relative_to(crate.cargo_toml.parent)
    build_options = '.define("main", "_main")' if crate.is_bin else ""
    build_rs_path = crate.cargo_toml.parent / "build.rs"
    build_rs_path.write_text(
        f"""
        fn main() {{
            println!("cargo:rerun-if-changed={c_src_path}");
            cc::Build::new()
                .compiler("clang")
                .warnings(false)
                .file("{c_src_path}")
                {build_options}
                .compile("library");
            println!("cargo:rustc-link-lib=static=library");
            // FIXME: How do we statically add libraries to link to?
            println!("cargo:rustc-link-lib=dylib=crypto");
        }}
        """
    )
    return build_rs_path


def write_main_binding(crate: Crate) -> CodeRust:
    main_binding_path, main_function = _write_main_binding(
        crate.c_src_path, crate.rust_src_path.parent / "binding"
    )
    crate.vcs.add(main_binding_path)
    return CodeRust(main_function)


def _write_main_binding(c_src_path: Path, binding_dir: Path) -> tuple[Path, str]:
    main_binding = _get_linked_binding("_main", c_src_path, "-Dmain=_main")
    main_binding_path = binding_dir / "main.rs"
    main_binding_path.parent.mkdir(exist_ok=True, parents=True)
    main_binding_path.write_text(
        "\n\n".join(
            [
                "#![allow(unused_attributes)]",
                str(main_binding),
            ]
        )
    )
    # Return appropriate main function instead of writing to binding.rs
    if "fn _main()" in str(main_binding):
        main_function = textwrap.dedent(
            """
            pub fn main() {
                let ret = unsafe { binding::main::_main() };
                std::process::exit(ret);
            }
            """
        )
    else:
        main_function = textwrap.dedent(
            """
            pub fn main() {
                let mut args: Vec<_> = std::env::args().into_iter().map(|s| std::ffi::CString::new(s).unwrap().into_raw()).collect();
                let ret = unsafe { binding::main::_main(args.len() as i32, args.as_mut_ptr()) };
                std::process::exit(ret);
            }
            """
        )
    return main_binding_path, main_function


def _generate_binding(crate: Crate, symbol: Symbol) -> tuple[Path, str, str | None]:
    c_src_path = crate.c_src_path
    binding_dir = crate.rust_src_path.parent / "binding"
    symbol_spelling = symbol.spelling
    is_main = crate.is_bin and symbol_spelling == "main"

    logger.info(f"Generating binding for symbol '{symbol_spelling}' ...")
    if is_main:
        main_binding_path, main_function = _write_main_binding(c_src_path, binding_dir)
        return main_binding_path, "main", main_function

    rust_spelling = mangle(symbol_spelling)
    symbol_binding = _get_linked_binding(rust_spelling, c_src_path)
    symbol_binding_path = binding_dir / f"{rust_spelling}.rs"
    symbol_binding_path.parent.mkdir(exist_ok=True, parents=True)
    symbol_binding_path.write_text(
        "\n\n".join(
            [
                "#![allow(unused_attributes)]",
                str(symbol_binding),
            ]
        )
    )
    return symbol_binding_path, rust_spelling, None


def _get_linked_binding(function_name: str, c_src_path: Path, *bindgen_args: str) -> CodeRust:
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

    # Enable the symbol to be re-exportable by rustc
    linked_binding = re.sub(
        r'unsafe extern "C" {\n(.*)\n}',
        r'#[link(name="library", kind="static")]\nunsafe extern "C" {\n    #[unsafe(no_mangle)]\n\1\n}',
        linked_binding,
        flags=re.DOTALL,
    )
    if linked_binding == binding:
        raise ValueError(
            f"Failed to convert binding to linked binding for {function_name}!\n{binding}"
        )
    return CodeRust(linked_binding)


def _main(cfg: BuildConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)

    # Fetch crate
    crate = Crate(cfg.cargo_toml, vcs=cfg.vcs)  # type: ignore[reportArgumentType]

    # Get global symbol table
    tu = create_translation_unit(crate.c_src_path)
    asts = [extract_info_c(tu)]
    symbols, _ = get_symbols_and_dependencies(
        asts, external_symbol_names=["c:@F@main"] if crate.is_bin else None
    )
    global_functions = [
        s for s in symbols.values() if s.is_global and (s.is_function and s.is_definition)
    ]
    if not global_functions:
        logger.info("No global functions to generate bindings for!")
        return

    # Write build.rs file
    build_rs_path = write_build_script(crate)
    crate.vcs.add(build_rs_path)

    # Verify build with build.rs
    builds, feedback = crate.cargo_build()
    if not builds:
        raise RuntimeError(f"Crate does not build!\n{feedback}")

    # Generate a Rust binding for any global function since we need to force the Rust
    # linker to include that C function in the Rust artifact.
    # FIXME: If we ever test variables we should generate bindings for those here too!
    bindings: list[tuple[Path, str, str | None]] = []
    if len(global_functions) <= 1:
        bindings.append(_generate_binding(crate, global_functions[0]))
    else:
        with ThreadPoolExecutor() as pool:
            futures = [
                pool.submit(_generate_binding, crate, symbol) for symbol in global_functions
            ]
            bindings = [future.result() for future in futures]

    # Write bindings to file and stage with VCS
    binding_modules_by_file: dict[Path, str] = {}
    for binding_file_path, rust_spelling, main_function in bindings:
        if main_function is not None:
            with crate.rust_src_path.open("a+") as rust_file:
                rust_file.write(main_function)
        binding_modules_by_file[binding_file_path] = f"pub mod {rust_spelling};\n"
    binding_path = crate.rust_src_path.parent / "binding.rs"
    binding_path.write_text("".join(binding_modules_by_file.values()))
    crate.vcs.add(*binding_modules_by_file.keys(), crate.rust_src_path, binding_path)

    # Make the bindings module visible in the crate
    rust_src = crate.rust_src_path.read_text()
    BINDING_MOD = "pub mod binding;"
    if not re.search(f"^{re.escape(BINDING_MOD)}$", rust_src, flags=re.MULTILINE):
        crate.rust_src_path.write_text("\n\n".join([rust_src, BINDING_MOD]))
    crate.vcs.add(crate.rust_src_path)

    # Add hydra directory
    if (output_subdir := HydraConfig.get().output_subdir) is not None:
        crate.vcs.add(output_dir / output_subdir)

    # Attempt a final build
    builds, feedback = crate.cargo_build()
    if not builds:
        raise RuntimeError(f"Crate does not build!\n{feedback}")
    msg = f"Generated build artifacts for `{crate.root_package['name']}`"
    logger.info(msg)
    crate.vcs.commit(msg)

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
