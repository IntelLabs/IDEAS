#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import logging
import textwrap
from pathlib import Path

import dspy

from .tools import Crate, run_subprocess
from .ast import Symbol, clang_make_extern_

logger = logging.getLogger("ideas.test_symbol")


class SymbolTester(dspy.Module):
    def __init__(self, crate: Crate, symbols: list[Symbol]):
        super().__init__()
        self.crate = crate

        # Write a build script to compile C code as a static library and link to it
        self.write_build_script_()

        # Generate a Rust binding for any global function since we need to force the Rust
        # linker to include that C function in the Rust artifact.
        # FIXME: If we ever test variables we should generate bindings for those here too!
        binding_path = self.crate.rust_src_path.parent / "binding.rs"
        binding_path.write_text("")
        self.main_function = ""
        for symbol in symbols:
            if not (symbol.is_function and symbol.is_definition and symbol.is_global):
                continue
            if self.crate.is_bin and symbol.spelling == "main":
                # main requires special handling because we must bind to it as _main and
                # statically create a Rust main that calls it
                self.main_function = self.write_main_binding()
            else:
                self.write_symbol_binding_(symbol.spelling)

        # These files are modified by test
        orig_binding_src = binding_path.read_bytes()
        orig_rust_src = self.crate.rust_src_path.read_bytes()

        # Check whether all of the changes compile and commit them
        passes, output = self.test()
        msg = f"Prepared `{self.crate.root_package['name']}` for symbol testing!"
        if not passes:
            msg = f"Failed to prepare `{self.crate.root_package['name']}` for symbol testing!"
        self.crate.vcs.commit(msg)

        # Restore originals
        binding_path.write_bytes(orig_binding_src)
        self.crate.rust_src_path.write_bytes(orig_rust_src)

        # Error loudly if changes don't build
        if not passes:
            msg += output
            raise ValueError(msg)

    def write_build_script_(self):
        c_src_path = self.crate.c_src_path.relative_to(self.crate.cargo_toml.parent)
        build_options = '.define("main", "_main")' if self.crate.is_bin else ""
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

        build_rs_path = self.crate.cargo_toml.parent / "build.rs"
        build_rs_path.write_text(build_rs_src)
        self.crate.vcs.add(build_rs_path)

    def write_symbol_binding_(self, symbol_name: str):
        symbol_binding = get_linked_binding(symbol_name, self.crate.c_src_path)

        symbol_binding_path = self.crate.rust_src_path.parent / "binding" / f"{symbol_name}.rs"
        symbol_binding_path.parent.mkdir(exist_ok=True)
        symbol_binding_path.write_text(symbol_binding)
        self.crate.vcs.add(symbol_binding_path)

        binding_path = self.crate.rust_src_path.parent / "binding.rs"
        with binding_path.open("a+") as f:
            f.write(f"pub mod {symbol_name};\n")
        self.crate.vcs.add(binding_path)

    def write_main_binding(self) -> str:
        # Get binding for main (redefined as _main)
        main_binding = get_linked_binding("_main", self.crate.c_src_path, "-Dmain=_main")

        main_binding_path = self.crate.rust_src_path.parent / "binding" / "main.rs"
        main_binding_path.parent.mkdir(exist_ok=True)
        main_binding_path.write_text(main_binding)
        self.crate.vcs.add(main_binding_path)

        # Return appropriate main function instead of writing to binding.rs
        if "fn _main()" in main_binding:
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

    def test(self) -> tuple[bool, str]:
        rust_src = self.crate.rust_src_path.read_text()

        # Remove forbid unsafe from Rust source
        rust_src = rust_src.replace("#![forbid(unsafe_code)]", "")

        # Replace Rust Mutex with C ABI-compatible Mutex in Rust source
        RUST_MUTEX = "use std::sync::{Mutex, MutexGuard};"
        C_ABI_MUTEX = "mod sync;\nuse crate::sync::{Mutex, MutexGuard};"
        rust_src = rust_src.replace(RUST_MUTEX, C_ABI_MUTEX)

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
        builds, feedback = self.crate.cargo_build(allow_unsafe=True, fix_E0601=False)
        if "error[E0601]" in feedback and self.main_function:
            with binding_path.open("a+") as f:
                f.write("pub mod main;\n")
            with self.crate.rust_src_path.open("a+") as f:
                f.write(self.main_function)

        self.crate.vcs.add(wrapper_path, binding_path, self.crate.rust_src_path)

        # Make sure the crate builds before testing
        builds, feedback = self.crate.cargo_build(allow_unsafe=True, fix_E0601=False)
        if not builds:
            raise RuntimeError(f"Crate does not build!\n{feedback}")
        passes, output, error, _ = self.crate.cargo_test()
        return passes, output + error

    def forward(self, symbol: Symbol) -> dspy.Prediction:
        logger.info(f"Testing symbol `{symbol.name}` ....")

        # Overwrite C symbol to reference extern symbol that we will link to the Rust symbol.
        # It is very important that this happens first since it will overwrite any other changes
        # made to the C code.
        clang_make_extern_(self.crate.c_src_path, symbol.spelling)
        self.crate.vcs.add(self.crate.c_src_path)

        # These files are modified by test
        binding_path = self.crate.rust_src_path.parent / "binding.rs"
        orig_binding_src = binding_path.read_bytes()
        orig_rust_src = self.crate.rust_src_path.read_bytes()

        # Run cargo test
        passes, feedback = self.test()
        msg = f"Tested symbol `{symbol.name}`"
        if not passes:
            feedback = "Running `cargo test` fails!\n" + feedback
            msg = f"Failed to test symbol `{symbol.name}`"
            logger.error(msg)
            msg += f"\n\n{feedback}"
        self.crate.vcs.commit(msg)

        # Restore originals
        binding_path.write_bytes(orig_binding_src)
        self.crate.rust_src_path.write_bytes(orig_rust_src)

        pred = dspy.Prediction(success=passes)
        if not passes:
            # FIXME: Use test feedback?
            pred.feedback = "Carefully compare the Rust translation in `prior_translation` with the C `snippet` and find where any mis-translations happen. Then use this knowledge to generate a correct Rust `translation` of the C `snippet`. You should treat the C `snippet` as correct, so if the C `snippet` has a bug, you should replicate that bug in the Rust `translation` too."
        return pred


def get_linked_binding(function_name: str, c_src_path: Path, *bindgen_args: str) -> str:
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

    # Parse binding since we need to add special link instructions
    linked_binding = re.sub(
        r'unsafe extern "C" {\n(.*)\n}',
        r'#[link(name="library", kind="static")]\nunsafe extern "C" {\n    #[unsafe(no_mangle)]\n\1\n}',
        binding,
        flags=re.DOTALL,
    )
    if linked_binding == binding:
        raise ValueError(
            f"Failed to convert binding to linked binding for {function_name}!\n{binding}"
        )
    return linked_binding
