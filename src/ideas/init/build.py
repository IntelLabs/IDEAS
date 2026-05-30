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

import hydra
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas.tools import Crate, LARGE_PROJECT
from ideas.tools import run_subprocess
from ideas.ast_rust import CodeRust, mangle
from ideas import create_translation_unit, extract_info_c
from ideas.init.consolidate import get_symbols_and_dependencies

logger = logging.getLogger("ideas.init.build")


@dataclass
class BuildConfig:
    vcs: str = "none"

    def __post_init__(self):
        if self.vcs not in ["git", "none"]:
            raise ValueError(f"Invalid VCS: {self.vcs}!")


cs = ConfigStore.instance()
cs.store(name="init.build", node=BuildConfig)


def write_build_script(crate: Crate) -> Path:
    c_src_path = crate.c_src_path.relative_to(crate.cargo_toml.parent)
    build_options = '.define("main", "_main")' if crate.is_bin else ""
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
            println!("cargo:rustc-link-lib=static=library");
            // FIXME: How do we statically add libraries to link to?
            println!("cargo:rustc-link-lib=dylib=crypto");
        }}
        """
    )

    build_rs_path = crate.cargo_toml.parent / "build.rs"
    build_rs_path.write_text(build_rs_src)
    return build_rs_path


def write_main_binding(crate: Crate) -> str:
    # Get binding for main (redefined as _main)
    main_binding = get_linked_binding("_main", crate.c_src_path, "-Dmain=_main")

    main_binding_path = crate.rust_src_path.parent / "binding" / "main.rs"
    main_binding_path.parent.mkdir(exist_ok=True)
    main_binding_path.write_text(
        "\n\n".join(
            [
                "#![allow(unused_attributes)]",
                main_binding.text,
            ]
        )
    )
    crate.vcs.add(main_binding_path)

    # Return appropriate main function instead of writing to binding.rs
    if "fn _main()" in main_binding.text:
        return textwrap.dedent(
            """
            pub fn main() {
                let ret = unsafe { binding::main::_main() };
                std::process::exit(ret);
            }
            """
        )
    else:
        return textwrap.dedent(
            """
            pub fn main() {
                let mut args: Vec<_> = std::env::args().into_iter().map(|s| std::ffi::CString::new(s).unwrap().into_raw()).collect();
                let ret = unsafe { binding::main::_main(args.len() as i32, args.as_mut_ptr()) };
                std::process::exit(ret);
            }
            """
        )


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

    if LARGE_PROJECT:
        logger.info("Hybrid build is disabled; skipping build.rs generation!")
        return

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
    if not global_functions:
        logger.info("No global functions to generate bindings for!")
        return

    # Write build.rs file
    build_rs_path = write_build_script(crate)
    crate.vcs.add(build_rs_path)

    # Verify build with build.rs
    builds, feedback = crate.cargo_build()
    if not builds:
        raise RuntimeError(f"Crate at {output_dir} does not build with build.rs!\n{feedback}")

    # Generate a Rust binding for any global function since we need to force the Rust
    # linker to include that C function in the Rust artifact.
    # FIXME: If we ever test variables we should generate bindings for those here too!
    binding_path = crate.rust_src_path.parent / "binding.rs"
    binding_path.write_text("")
    main_function = ""
    for symbol in global_functions:
        if not (symbol.is_function and symbol.is_definition and symbol.is_global):
            continue
        if crate.is_bin and symbol.spelling == "main":
            # main requires special handling because we must bind to it as _main and
            # statically create a Rust main that calls it
            main_function = write_main_binding(crate)
        else:
            write_symbol_binding(crate, symbol.spelling)

    # Write main function and binding to it
    with crate.rust_src_path.open("a+") as f:
        f.write(main_function)
    if main_function:
        with binding_path.open("a+") as f:
            f.write("pub mod main;\n")
    crate.vcs.add(crate.rust_src_path)
    crate.vcs.add(binding_path)

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
        raise RuntimeError(f"Crate at {output_dir} does not build with build.rs!\n{feedback}")
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
