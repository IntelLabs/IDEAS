#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from textwrap import dedent

from .ast import Symbol, SymbolName
from .ast import clang_make_global_, clang_make_extern_
from .ast import clang_function_arity, clang_function_definitions, clang_rename_
from .ast_rust import BindgenName, CodeRust, get_root, get_referenced_type_names
from .tools import Crate, REDUCED_CONTEXT
from .wrapper import C_INTEROP_TRAIT, TYPES_PREAMBLE, wrap_values
from .wrapper import bindgen_types, bindgen_bindings, split_items
from .wrapper import generate_unimplemented_function_wrapper


@dataclass(frozen=True)
class HybridState:
    types: Mapping[BindgenName, CodeRust] = field(default_factory=dict)
    consts: Mapping[BindgenName, CodeRust] = field(default_factory=dict)
    externs: tuple[CodeRust, ...] = ()
    wrappers: Mapping[SymbolName, CodeRust] = field(default_factory=dict)
    main_wrapped: bool = False
    c_stranded: bool = False

    @property
    def types_code(self) -> CodeRust:
        return CodeRust.join(self.types.values())

    def type_slice(
        self, bindgen_name: BindgenName | None, wrapper: CodeRust
    ) -> tuple[Iterable[CodeRust], Iterable[CodeRust]]:
        if not REDUCED_CONTEXT:
            # Return all types because the wrapper may reference any of them
            return self.types.values(), self.consts.values()

        # In reduced context mode, keep only the types reachable from this symbol, the externs
        # and its own wrapper
        pending = {TYPES_PREAMBLE}
        if bindgen_name is not None:
            pending.add(bindgen_name)
        for code in (*self.externs, wrapper):
            pending |= get_referenced_type_names(get_root(str(code)))

        keep: set[BindgenName] = set()
        while pending:
            name = pending.pop()
            if name in keep or (item := self.types.get(name)) is None:
                continue
            keep.add(name)
            pending |= get_referenced_type_names(get_root(str(item)))
        return (
            [code for name, code in self.types.items() if name in keep],
            [code for name, code in self.consts.items() if name in keep],
        )

    def render(
        self,
        wrappers: Iterable[CodeRust] | None = None,
        types: Iterable[CodeRust] | None = None,
        consts: Iterable[CodeRust] | None = None,
    ) -> CodeRust:
        if wrappers is None:
            wrappers = self.wrappers.values()
        if types is None:
            types = self.types.values()
        if consts is None:
            consts = self.consts.values()
        return CodeRust.join(
            [
                C_INTEROP_TRAIT,
                CodeRust.join(types),
                wrap_values(CodeRust.join(consts), *self.externs),
                *wrappers,
            ]
        )


@dataclass(frozen=True)
class HybridSnapshot:
    state: HybridState
    c_src: bytes


class HybridWriter:
    def __init__(self, sys_crate: Crate, crate: Crate, rust_lib_name: str):
        if crate.lib_src_path is None and crate.main_src_path is None:
            raise ValueError(f"Crate {crate.name} has neither lib.rs nor main.rs!")
        assert sys_crate.lib_src_path is not None
        assert sys_crate.lib_name is not None
        self._crate = crate
        self._c_src_path = sys_crate.lib_src_path.with_suffix(".c")
        self._sys_lib_name = sys_crate.lib_name
        self._rust_lib_name = rust_lib_name
        self._state = HybridState()
        self._bindings: dict[SymbolName, CodeRust] = {}
        if crate.main_src_path is not None:
            self._adopt_c_entrypoint()
        self._flush()

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
    def main_wrapped(self) -> bool:
        return self._state.main_wrapped

    @property
    def types_code(self) -> CodeRust:
        return self._state.types_code

    @property
    def bindings(self) -> Mapping[SymbolName, CodeRust]:
        return self._bindings

    def type_slice(
        self, bindgen_name: BindgenName | None, wrapper: CodeRust
    ) -> tuple[Iterable[CodeRust], Iterable[CodeRust]]:
        return self._state.type_slice(bindgen_name, wrapper)

    def render(
        self,
        wrappers: Iterable[CodeRust] | None = None,
        types: Iterable[CodeRust] | None = None,
        consts: Iterable[CodeRust] | None = None,
    ) -> CodeRust:
        return self._state.render(wrappers, types, consts)

    def set_types(
        self, types: Mapping[BindgenName, CodeRust], consts: Mapping[BindgenName, CodeRust]
    ):
        self._state = replace(self._state, types=types, consts=consts)
        self._flush()

    def add_externs(self, externs: Iterable[CodeRust]):
        self._state = replace(self._state, externs=(*self._state.externs, *externs))
        self._flush()

    def write_types(self, symbols: Iterable[Symbol]):
        # An anonymous record has no name to allowlist, so bindgen only reaches it through
        # whatever names the field that uses it
        items = bindgen_types(
            self._c_src_path, [n for s in symbols if (n := s.bindgen_name) is not None]
        )
        types, consts = split_items(items)
        self.set_types(types, consts)

    def add_bindings(self, symbols: Iterable[Symbol]) -> list[str]:
        variables = [s for s in symbols if s.is_variable]
        if not variables:
            return []

        clang_make_global_(self._c_src_path, [s.spelling for s in variables])
        bindings = bindgen_bindings(
            self._c_src_path, [s.spelling for s in variables], self._state.types_code
        )
        for symbol in variables:
            self._bindings[symbol.name] = bindings[symbol.spelling]
        self.add_externs([bindings[s.spelling] for s in variables])

        self._crate.vcs.add(self._c_src_path)
        return [s.spelling for s in variables]

    def set_wrapper(self, name: SymbolName, wrapper: CodeRust):
        self._state = replace(self._state, wrappers={**self._state.wrappers, name: wrapper})
        self._flush()

    def take_over_main(self):
        # Declare the C entrypoint chain as extern so Rust owns the definition and we avoid
        # duplicate entrypoint symbols at link time.
        clang_make_extern_(self._c_src_path, "main")
        clang_make_extern_(self._c_src_path, "__ideas_entry")
        clang_make_extern_(self._c_src_path, "__ideas_c_main")
        self._crate.vcs.add(self._c_src_path)

        self._state = replace(self._state, main_wrapped=True)
        self._flush()

    def make_extern(self, spelling: str):
        clang_make_extern_(self._c_src_path, spelling)
        self._crate.vcs.add(self._c_src_path)

    def strand_c(self):
        # Drop every C body no wrapper replaced so nothing can link the originals. Whatever the
        # translation did reach survives, but it can no longer be propped up by the C it skipped.
        clang_make_extern_(self._c_src_path, clang_function_definitions(self._c_src_path))
        self._crate.vcs.add(self._c_src_path)

        self._state = replace(self._state, c_stranded=True)
        self._flush()

    def unimplemented_function_wrapper(self, spelling: str) -> CodeRust | None:
        return generate_unimplemented_function_wrapper(
            self._c_src_path, spelling, self._state.types_code
        )

    def snapshot(self) -> HybridSnapshot:
        return HybridSnapshot(state=self._state, c_src=self._c_src_path.read_bytes())

    def restore(self, snapshot: HybridSnapshot):
        self._state = snapshot.state
        self._flush()
        self._c_src_path.write_bytes(snapshot.c_src)
        self._crate.vcs.add(self._c_src_path)

    def _adopt_c_entrypoint(self):
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
        # Link the -sys crate to resolve the C symbols no wrapper replaced; rustc drops it
        # unless something names it, and wrapping is never exhaustive
        body = CodeRust(f"use {self._sys_lib_name} as _;")
        body += self._state.render()

        if (lib_src_path := self._crate.lib_src_path) is not None:
            lib_src_path.parent.mkdir(parents=True, exist_ok=True)
            lib_src_path.write_text(str(body))
            self._crate.vcs.add(lib_src_path)

        if (main_src_path := self._crate.main_src_path) is not None:
            if self._state.main_wrapped:
                # C's `main` is now extern-only, so the -rs crate supplies the entrypoint
                root = CodeRust(f"use {self._rust_lib_name}::main;")
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
                        let mut argv: Vec<*mut c_char> =
                            args.iter_mut().map(|a| a.as_mut_ptr().cast()).collect();
                        argv.push(std::ptr::null_mut());
                        let code = unsafe { __ideas_entry(argc, argv.as_mut_ptr()) };
                        std::process::ExitCode::from(code as u8)
                    }
                    """)
                )

            if lib_src_path is not None:
                # Compile lib.rs into the bin rather than depend on it
                root += CodeRust(f'#[path = "{lib_src_path.name}"]\nmod hybrid;')
            else:
                root += body

            main_src_path.parent.mkdir(parents=True, exist_ok=True)
            main_src_path.write_text(str(root))
            self._crate.vcs.add(main_src_path)
