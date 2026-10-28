#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import logging
from pathlib import Path
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from textwrap import dedent

from .ast import Symbol, SymbolName
from .ast import clang_make_global_, clang_make_extern_
from .ast import clang_function_arity, clang_function_definitions, clang_rename_
from .ast_rust import BindgenName, CodeRust, strip_cfg_test, strip_exports, strip_visibility
from .tools import Crate
from .bindgen import C_INTEROP_TRAIT, wrap_values
from .bindgen import bindgen_types, bindgen_bindings, split_items

logger = logging.getLogger("ideas.hybrid")

# Safe crate alias for other crates
SAFE_CRATE = "safe_crate"


@dataclass(frozen=True)
class HybridState:
    types: Mapping[BindgenName, CodeRust] = field(default_factory=dict)
    consts: Mapping[BindgenName, CodeRust] = field(default_factory=dict)
    externs: tuple[CodeRust, ...] = ()
    wrappers: Mapping[SymbolName, CodeRust] = field(default_factory=dict)
    scaffolding: frozenset[SymbolName] = frozenset()
    main_from: str | None = None
    c_stranded: bool = False

    @property
    def types_code(self) -> CodeRust:
        return CodeRust.join(self.types.values())

    def render(self, *, export_scaffolding: bool = True) -> CodeRust:
        if export_scaffolding:
            wrappers = list(self.wrappers.values())
        else:
            # Tests already passed against the hybrid; scaffolding may still be installed as a
            # callback by other wrappers, so it stays but is not exported
            wrappers = [
                strip_cfg_test(
                    strip_visibility(strip_exports(w)) if n in self.scaffolding else w
                )
                for n, w in self.wrappers.items()
            ]
        return CodeRust.join(
            [
                C_INTEROP_TRAIT,
                self.types_code,
                wrap_values(CodeRust.join(self.consts.values()), *self.externs),
                *wrappers,
            ]
        )


@dataclass(frozen=True)
class HybridSnapshot:
    state: HybridState
    c_src: bytes


class Hybrid:
    def __init__(
        self,
        crate: Crate,
        sys_crate: Crate,
        ffi_crate: Crate | None = None,
    ):
        if crate.lib_src_path is None and crate.main_src_path is None:
            raise ValueError(f"Crate {crate.name} has neither lib.rs nor main.rs!")
        assert sys_crate.lib_name is not None
        assert sys_crate.lib_src_path is not None
        self._crate = crate
        self._ffi_crate = ffi_crate
        self._c_src_path = sys_crate.lib_src_path.with_suffix(".c")
        self._sys_lib_name = sys_crate.lib_name
        self._state = HybridState()
        self._bindings: dict[SymbolName, CodeRust] = {}
        self._flush()

    @property
    def crate(self) -> Crate:
        return self._crate

    @property
    def types(self) -> Mapping[BindgenName, CodeRust]:
        return self._state.types

    @property
    def consts(self) -> Mapping[BindgenName, CodeRust]:
        return self._state.consts

    @property
    def externs(self) -> tuple[CodeRust, ...]:
        return self._state.externs

    @property
    def wrappers(self) -> Mapping[SymbolName, CodeRust]:
        return self._state.wrappers

    @property
    def types_code(self) -> CodeRust:
        return self._state.types_code

    @property
    def bindings(self) -> Mapping[SymbolName, CodeRust]:
        return self._bindings

    @property
    def c_src_path(self) -> Path:
        return self._c_src_path

    def render(self) -> CodeRust:
        return self._state.render()

    def set_types(
        self, types: Mapping[BindgenName, CodeRust], consts: Mapping[BindgenName, CodeRust]
    ):
        self._state = replace(self._state, types=types, consts=consts)
        self._flush()

    def add_externs(self, externs: Iterable[CodeRust]):
        self._state = replace(self._state, externs=(*self._state.externs, *externs))
        self._flush()

    def import_symbols(self, symbols: Iterable[Symbol]):
        symbols = list(symbols)
        # Variables' bindings name the imported types
        self._import_types(symbols)
        self._import_variables(symbols)

    def _import_types(self, symbols: list[Symbol]):
        # An anonymous record has no name to allowlist, so bindgen only reaches it through
        # whatever names the field that uses it
        items = bindgen_types(
            self._c_src_path, [n for s in symbols if (n := s.bindgen_name) is not None]
        )
        types, consts = split_items(items)
        self.set_types(types, consts)

        # bindgen renames nested records after their whole ancestor chain, so a wrong
        # `bindgen_name` silently yields a type slice that never declares the item the
        # wrapper will `impl CInterop for`
        missing = sorted(
            {
                bindgen_name
                for s in symbols
                if s.is_struct and s.is_externally_visible
                if (bindgen_name := s.bindgen_name) is not None and bindgen_name not in types
            }
        )
        if missing:
            logger.warning(
                f"bindgen did not declare wrappable type(s): {', '.join(missing)}. "
                "Wrapping them will fail to build."
            )

    def _import_variables(self, symbols: list[Symbol]):
        variables = [s for s in symbols if s.is_variable]
        if not variables:
            return

        clang_make_global_(self._c_src_path, [s.spelling for s in variables])
        bindings = bindgen_bindings(
            self._c_src_path, [s.spelling for s in variables], self._state.types_code
        )
        for symbol in variables:
            self._bindings[symbol.name] = bindings[symbol.spelling]
        self.add_externs([bindings[s.spelling] for s in variables])

        self._crate.vcs.add(self._c_src_path)

    def wrap(
        self,
        name: SymbolName,
        wrapper: CodeRust,
        *,
        c_symbol: str | None = None,
        scaffolding: bool = False,
    ):
        # Hands `name` to Rust; a wrapper exporting `c_symbol` needs C's definition gone or the
        # two collide at link time
        if c_symbol is not None:
            clang_make_extern_(self._c_src_path, c_symbol)
            self._crate.vcs.add(self._c_src_path)
        names = self._state.scaffolding - {name}
        if scaffolding:
            names |= {name}
        self._state = replace(
            self._state,
            wrappers={**self._state.wrappers, name: wrapper},
            scaffolding=names,
        )
        self._flush()

    def take_over_main(self):
        # Declare the C entrypoint chain as extern so Rust owns the definition and we avoid
        # duplicate entrypoint symbols at link time.
        clang_make_extern_(self._c_src_path, "main")
        clang_make_extern_(self._c_src_path, "__ideas_entry")
        clang_make_extern_(self._c_src_path, "__ideas_c_main")
        self._crate.vcs.add(self._c_src_path)

        self._state = replace(self._state, main_from=SAFE_CRATE)
        self._flush()

    def strand_c(self):
        # Drop every C body no wrapper replaced so nothing can link the originals. Whatever the
        # translation did reach survives, but it can no longer be propped up by the C it skipped.
        clang_make_extern_(self._c_src_path, clang_function_definitions(self._c_src_path))
        self._crate.vcs.add(self._c_src_path)

        self._state = replace(self._state, c_stranded=True)
        self._flush()

    def snapshot(self) -> HybridSnapshot:
        return HybridSnapshot(state=self._state, c_src=self._c_src_path.read_bytes())

    def restore(self, snapshot: HybridSnapshot):
        self._state = snapshot.state
        self._flush()
        self._c_src_path.write_bytes(snapshot.c_src)
        self._crate.vcs.add(self._c_src_path)

    def adopt_c_entrypoint(self):
        # Free the `main` symbol for Rust to define; C's program logic lives on under the new name
        clang_rename_(self._c_src_path, {"main": "__ideas_c_main"})

        # Emitting the call in C lets the C compiler check it against the real prototype, so a
        # misread `main` signature is a build error instead of silent UB
        match clang_function_arity(self._c_src_path, "__ideas_c_main"):
            case 0:
                entry = """
                    int __ideas_entry(int argc, char **argv) {
                        return __ideas_c_main();
                    }
                    """
            case 2:
                entry = """
                    int __ideas_entry(int argc, char **argv) {
                        return __ideas_c_main(argc, argv);
                    }
                    """
            case 3:
                entry = """
                    extern char **environ;

                    int __ideas_entry(int argc, char **argv) {
                        return __ideas_c_main(argc, argv, environ);
                    }
                    """
            case nparams:
                raise NotImplementedError(f"Unhandled `main` parameter count {nparams}!")

        with self._c_src_path.open("a") as c_src:
            c_src.write(dedent(entry))

            # Keep the -sys crate's bin linkable on its own; in the hybrid the linker prefers
            # Rust's strong `main` and this trampoline goes unused
            c_src.write(
                dedent(
                    """
                    __attribute__((weak)) int main(int argc, char **argv) {
                        return __ideas_entry(argc, argv);
                    }
                    """
                )
            )
        self._crate.vcs.add(self._c_src_path)

    def _flush(self):
        # Preserve C APIs while forbidding unsound signatures and undocumented or overly broad
        # unsafe operations. Clippy warnings are feedback, not a correctness gate.
        crate_allow = CodeRust(
            "#![forbid(\n"
            "    improper_ctypes,\n"
            "    improper_ctypes_definitions,\n"
            "    static_mut_refs,\n"
            "    unsafe_op_in_unsafe_fn,\n"
            "    clippy::multiple_unsafe_ops_per_block,\n"
            "    clippy::not_unsafe_ptr_arg_deref,\n"
            "    clippy::undocumented_unsafe_blocks\n"
            ")]"
        )

        # Link the -sys crate to resolve the C symbols no wrapper replaced; rustc drops it
        # unless something names it, and wrapping is never exhaustive
        link_sys = CodeRust(f"use {self._sys_lib_name} as _;")
        body = link_sys + self._state.render()

        if (lib_src_path := self._crate.lib_src_path) is not None:
            lib_src_path.parent.mkdir(parents=True, exist_ok=True)
            lib_src_path.write_text(str(crate_allow + body))
            self._crate.vcs.add(lib_src_path)

        if (main_src_path := self._crate.main_src_path) is not None:
            if (main_from := self._state.main_from) is not None:
                # C's `main` is now extern-only, so the -rs crate supplies the entrypoint
                root = CodeRust(f"use {main_from}::main;")
            elif self._state.c_stranded:
                # `__ideas_entry` went with the rest of the C, so the binary has to be able to
                # link without it; failing loudly beats exiting 0 without running anything
                root = CodeRust(
                    dedent("""\
                    fn main() {
                        unimplemented!("`main` was never translated to Rust");
                    }
                    """)
                )
            else:
                # C still owns the program logic, but Rust owns the entrypoint and calls in.
                root = CodeRust(
                    dedent("""\
                    unsafe extern "C" {
                        fn __ideas_entry(argc: std::ffi::c_int, argv: *mut *mut std::ffi::c_char) -> std::ffi::c_int;
                    }

                    fn main() -> std::process::ExitCode {
                        use std::ffi::{c_char, c_int, CString};

                        let mut args: Vec<Vec<u8>> = std::env::args_os()
                            .map(|arg| {
                                CString::new(arg.into_encoded_bytes())
                                    .expect("NUL in argument")
                                    .into_bytes_with_nul()
                            })
                            .collect();
                        let argc = args.len() as c_int;
                        let mut argv: Vec<*mut c_char> = args.iter_mut().map(|a| a.as_mut_ptr().cast()).collect();
                        argv.push(std::ptr::null_mut());
                        // SAFETY: `args` owns the NUL-terminated strings and remains alive for the call.
                        //         `argv` points to those strings and ends with a null pointer.
                        let code = unsafe { __ideas_entry(argc, argv.as_mut_ptr()) };
                        std::process::ExitCode::from(code as u8)
                    }
                    """)
                )

            if lib_src_path is not None:
                # Compile lib.rs into the bin as a private module instead of depending on the lib
                # crate. The crate-root warning policy applies to this module too.
                root += CodeRust(f'#[path = "{lib_src_path.name}"]\nmod hybrid;')
            else:
                root += body

            root = crate_allow + root
            main_src_path.parent.mkdir(parents=True, exist_ok=True)
            main_src_path.write_text(str(root))
            self._crate.vcs.add(main_src_path)

        if self._ffi_crate is not None and (ffi_src_path := self._ffi_crate.lib_src_path):
            # After `strand_c`, -sys only holds the C globals' storage the sync helpers need
            ffi_body = crate_allow + link_sys + self._state.render(export_scaffolding=False)
            ffi_src_path.write_text(str(ffi_body))
            self._ffi_crate.vcs.add(ffi_src_path)
