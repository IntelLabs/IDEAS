#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import sys
import re
import json
import shlex
import logging
import argparse
from pathlib import Path
from dataclasses import dataclass
from functools import cached_property

from .tools import run_subprocess

logger = logging.getLogger("ideas.bear")

TargetName = str  # normalized target filename, e.g. "libfoo.so" or "mybin"


@dataclass
class CompileCommand:
    source: Path  # resolved absolute source path
    output: Path  # resolved absolute .o path
    arguments: list[str]
    working_dir: Path

    def to_dict(self) -> dict:
        return {
            "file": str(self.source),
            "arguments": self.arguments,
            "directory": str(self.working_dir),
            "output": str(self.output),
        }

    @staticmethod
    def _is_compile_command(arguments: list[str]) -> bool:
        if not arguments or "-c" not in arguments:
            return False
        executable = Path(arguments[0]).name
        return any(executable == c or executable.startswith(c + "-") for c in _COMPILERS)

    @classmethod
    def from_arguments(cls, arguments: list[str], working_dir: Path) -> "CompileCommand | None":
        if not cls._is_compile_command(arguments):
            return None
        obj_path = source_path = None
        filtered: list[str] = []
        i = 0
        while i < len(arguments):
            arg = arguments[i]
            if arg in _DEP_FLAGS_WITH_ARG:
                i += 2  # skip flag and its argument
            elif arg in _DEP_FLAGS:
                i += 1  # skip standalone flag
            else:
                filtered.append(arg)
                if arg == "-o" and i + 1 < len(arguments):
                    obj_path = (working_dir / Path(arguments[i + 1])).resolve()
                elif arg == "-c" and i + 1 < len(arguments):
                    p = Path(arguments[i + 1])
                    if p.suffix in _SOURCE_EXTS:
                        source_path = (working_dir / p).resolve()
                i += 1
        if obj_path and source_path:
            return cls(
                source=source_path, output=obj_path, arguments=filtered, working_dir=working_dir
            )
        return None


@dataclass
class LinkCommand:
    arguments: list[str]  # original argv from the linker invocation
    working_dir: Path

    @staticmethod
    def _is_link_command(arguments: list[str]) -> bool:
        if not arguments:
            return False
        if not _matches_tool(Path(arguments[0]).name, _LINKERS):
            return False
        # Must not be a compile-only step
        if "-c" in arguments:
            return False
        # Must have at least one object file input to distinguish from non-linker calls
        return any(arg.endswith(".o") for arg in arguments[1:])

    @cached_property
    def is_driver(self) -> bool:
        # gcc/clang rather than the ld they exec
        return _matches_tool(Path(self.arguments[0]).name, _COMPILERS)

    @classmethod
    def from_arguments(cls, arguments: list[str], working_dir: Path) -> "LinkCommand | None":
        if not cls._is_link_command(arguments):
            return None
        cmd = cls(arguments=arguments, working_dir=working_dir)
        if cmd.target is None:
            logger.warning("Linker invocation has no -o flag: %s", arguments[:6])
            return None
        return cmd

    @cached_property
    def _arg_pairs(self) -> list[tuple[str | None, str]]:
        # argv normalized to (flag, operand) pairs; the glued `-L/opt` and separated
        # `-L /opt` forms collapse to the same pair, and inputs come back as (None, arg)
        pairs: list[tuple[str | None, str]] = []
        it = iter(self.arguments[1:])  # skip argv[0]
        for arg in it:
            if arg in _IGNORED_FLAGS_WITH_ARG:
                next(it, None)
            elif flag := next((f for f in _LINK_FLAGS_WITH_ARG if arg.startswith(f)), None):
                if value := arg[len(flag) :] or next(it, ""):
                    pairs.append((flag, value))
            elif not arg.startswith("-"):
                pairs.append((None, arg))
        return pairs

    @cached_property
    def _output_arg(self) -> str | None:
        outputs = [v for flag, v in self._arg_pairs if flag == "-o"]
        # No build generator emits two -o, so this means flags were injected from elsewhere
        if len(outputs) > 1:
            logger.warning(
                "Linker invocation has %d -o flags (%s); using the last one as gcc/clang do",
                len(outputs),
                ", ".join(outputs),
            )
        return outputs[-1] if outputs else None

    @cached_property
    def target(self) -> TargetName | None:
        if self._output_arg is None:
            return None
        # Strip version suffix from shared libraries: libfoo.so.1.2.3 -> libfoo.so
        return re.sub(r"\.so(\.\d+)+$", ".so", Path(self._output_arg).name)

    @cached_property
    def output_path(self) -> Path | None:
        return None if self._output_arg is None else self._resolve(self._output_arg)

    def _resolve(self, path: str) -> Path:
        p = Path(path)
        return (p if p.is_absolute() else self.working_dir / p).resolve()

    @cached_property
    def object_files(self) -> list[Path]:
        return [
            self._resolve(v) for flag, v in self._arg_pairs if flag is None and v.endswith(".o")
        ]

    @cached_property
    def link_inputs(self) -> list[str | Path]:
        # `-l` names (str) and library path inputs (Path) interleaved in command-line order:
        # a library only resolves against inputs to its left, so the order is significant.
        inputs: list[str | Path] = []
        for flag, value in self._arg_pairs:
            if flag == "-l":
                inputs.append(value)
            elif flag is None and is_library_file(Path(value)):
                inputs.append(self._resolve(value))
        return inputs

    @cached_property
    def link_search_dirs(self) -> list[Path]:
        return [self._resolve(v) for flag, v in self._arg_pairs if flag == "-L"]


_SOURCE_EXTS = {".c", ".cpp", ".cxx", ".cc", ".C", ".s", ".S", ".m"}
_COMPILERS = frozenset({"gcc", "clang", "cc", "g++", "c++", "clang++"})
_LINKERS = _COMPILERS | frozenset({"ld", "ld.bfd", "ld.lld", "ld.gold", "lld"})
# CMake injects these dependency-tracking flags into every compile command.
# They cause libclang to try writing .d files at relative paths that don't
# exist during analysis, which fails TranslationUnit parsing.
_DEP_FLAGS = frozenset({"-MD", "-MMD", "-MP", "-MG"})
_DEP_FLAGS_WITH_ARG = frozenset({"-MF", "-MT", "-MQ"})
# Linker flags whose operand may be glued to the flag or a separate argv entry
_LINK_FLAGS_WITH_ARG = frozenset({"-o", "-L", "-l"})
# Flags that likewise take a separate operand, but one we have no use for. Unlike the set
# above these are matched exactly, never by prefix: the glued spellings that extend them
# (-Bstatic, -fPIC, -znow) take no operand and would swallow the input that follows.
_IGNORED_FLAGS_WITH_ARG = frozenset(
    {
        # file operands
        "-plugin",
        "-dynamic-linker",
        "--dynamic-linker",
        "-T",
        "--script",
        "-R",
        "--just-symbols",
        "-Map",
        "--version-script",
        "--dynamic-list",
        "--retain-symbols-file",
        "--out-implib",
        "--exclude-libs",
        "-F",
        "--filter",
        "-f",
        "--auxiliary",
        "--sysroot",
        "-rpath",
        "--rpath",
        "-rpath-link",
        "--rpath-link",
        # name, symbol and value operands
        "-h",
        "-soname",
        "--soname",
        "-e",
        "--entry",
        "-u",
        "--undefined",
        "-y",
        "--trace-symbol",
        "--wrap",
        "--defsym",
        "-m",
        "-z",
        "-Xlinker",
        "-B",
    }
)


@dataclass
class TargetOutputs:
    entries: list[CompileCommand]
    link_inputs: list[str | Path]  # `-l` names and library paths, in link order
    link_search_dirs: list[Path]  # the build's own -L flags


@dataclass
class BuildDatabase:
    compile_commands: list[CompileCommand]
    link_commands: list[LinkCommand]

    @classmethod
    def from_events(cls, events_path: Path) -> "BuildDatabase":
        compile_commands: list[CompileCommand] = []
        link_commands: list[LinkCommand] = []

        with events_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning("Skipping malformed events.jsonl line: %s", line[:80])
                    continue

                arguments = event.get("arguments", [])
                working_dir = Path(event.get("working_dir", "."))

                if cmd := LinkCommand.from_arguments(arguments, working_dir=working_dir):
                    link_commands.append(cmd)
                elif cmd := CompileCommand.from_arguments(arguments, working_dir=working_dir):
                    compile_commands.append(cmd)
                else:
                    logger.debug("Skipping event: %s", arguments[:3])

        return cls(compile_commands=compile_commands, link_commands=link_commands)

    def _collect_transitive_entries(
        self,
        link_cmd: "LinkCommand",
        obj_map: dict[Path, CompileCommand],
        link_map: dict[Path, "LinkCommand"],
    ) -> TargetOutputs:
        entries: list[CompileCommand] = []
        link_inputs: list[str | Path] = []
        search_dirs: list[Path] = []
        seen_sources: set[Path] = set()
        seen_binaries: set[Path] = set()

        def _collect(binary: Path) -> None:
            if binary in seen_binaries or (producer := link_map.get(binary)) is None:
                return
            seen_binaries.add(binary)
            # A flattened target inherits the link requirements of every library it absorbs
            search_dirs.extend(producer.link_search_dirs)
            for obj in producer.object_files:
                if (entry := obj_map.get(obj)) is not None:
                    # dedup by source path
                    if entry.source not in seen_sources:
                        seen_sources.add(entry.source)
                        entries.append(entry)
                elif obj.is_relative_to(Path("/usr")):
                    # System CRT objects (crti.o, crtbeginS.o, ...) have no compile command
                    logger.debug(
                        "Skipping system object file: %s (linked into %s)",
                        obj,
                        link_cmd.target,
                    )
                else:
                    logger.warning(
                        "No compile command found for object file: %s (linked into %s)",
                        obj,
                        link_cmd.target,
                    )
            for inp in producer.link_inputs:
                if isinstance(inp, Path) and inp in link_map:
                    # a .so/.a built in this project — recurse, splicing its own link
                    # requirements in at the position it occupied on the link line
                    _collect(inp)
                else:
                    # an external library: keep it, path and all
                    link_inputs.append(inp)

        if link_cmd.output_path:
            _collect(link_cmd.output_path)
        return TargetOutputs(
            entries=entries,
            link_inputs=list(dict.fromkeys(link_inputs)),
            link_search_dirs=list(dict.fromkeys(search_dirs)),
        )

    def _primary_link_commands(self) -> dict[Path, LinkCommand]:
        # A driver execs ld, so bear captures two commands for one link. Keep the driver's since
        # ld's line additionally carries CRT objects and the toolchain -l/-L the driver adds.
        primary: dict[Path, LinkCommand] = {}
        for lc in self.link_commands:
            if lc.output_path is None:
                continue
            prev = primary.get(lc.output_path)
            if prev is None or lc.is_driver or not prev.is_driver:
                primary[lc.output_path] = lc
        return primary

    def resolve_targets(self) -> dict[TargetName, TargetOutputs]:
        obj_map: dict[Path, CompileCommand] = {cmd.output: cmd for cmd in self.compile_commands}
        primary = self._primary_link_commands()
        link_map: dict[Path, LinkCommand] = dict(primary)
        for output_path, lc in primary.items():
            # Also index by the unversioned name (libfoo.so.1.2.3 → libfoo.so) so that
            # consumers referencing the symlink name are resolved transitively.
            normalized = output_path.parent / re.sub(r"\.so(\.\d+)+$", ".so", output_path.name)
            if normalized != output_path:
                link_map[normalized] = lc
        result: dict[TargetName, TargetOutputs] = {}
        for link_cmd in primary.values():
            assert link_cmd.target is not None
            outputs = self._collect_transitive_entries(link_cmd, obj_map, link_map)
            _warn_ambiguous_lib_files(link_cmd.target, outputs)
            result[link_cmd.target] = outputs
        return result

    def write_outputs(self, output_dir: Path) -> None:
        for target, outputs in self.resolve_targets().items():
            # CompilationDatabase.fromDirectory() requires the file to be named compile_commands.json
            # inside the directory passed to it. Use a <target>.d/ suffix to avoid colliding with
            # the build artifact (e.g. libcjson.so already exists as a file in the build dir).
            target_dir = output_dir / f"{target}.d"
            target_dir.mkdir(exist_ok=True)
            (target_dir / "compile_commands.json").write_text(
                json.dumps([e.to_dict() for e in outputs.entries], indent=2)
            )
            link_entries: list[dict] = [{"source": str(e.source)} for e in outputs.entries]
            link_entries += [{"search_dir": str(d)} for d in outputs.link_search_dirs]
            link_entries += [
                {"lib_path": str(i)} if isinstance(i, Path) else {"lib": i}
                for i in outputs.link_inputs
            ]
            (target_dir / "links.json").write_text(
                json.dumps({"entries": link_entries}, indent=2)
            )


def is_library_file(path: Path) -> bool:
    # libfoo.a, libfoo.so, versioned libfoo.so.1.2.13, and the unprefixed spelling
    # (foo.so) that a CMake MODULE library or a plugin gets
    return re.fullmatch(r".+\.(?:so(?:\.\d+)*|a)", path.name) is not None


def _matches_tool(basename: str, tools: frozenset[str]) -> bool:
    # exact names and versioned variants e.g. clang-21, gcc-13
    return any(basename == t or basename.startswith(t + "-") for t in tools)


def _warn_ambiguous_lib_files(target: TargetName, outputs: TargetOutputs) -> None:
    # rustc can only re-link a path-named library as `-l:<file>` against a search path, so
    # the exact directory is lost; warn when the filename is not unique across those dirs
    lib_paths = [i for i in outputs.link_inputs if isinstance(i, Path)]
    dirs = list(dict.fromkeys(outputs.link_search_dirs + [p.parent for p in lib_paths]))
    for path in lib_paths:
        matches = [d for d in dirs if (d / path.name).exists()]
        if len(matches) > 1:
            logger.warning(
                "%s: %s exists in %s — the linker resolves it to the first match",
                target,
                path.name,
                ", ".join(str(d) for d in matches),
            )


def _main(build_command: list[str], output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)

    # Run the build under bear intercept to capture all compiler and linker invocations.
    # events.jsonl contains every execve call: compilations, links, and system tools.
    events_path = output_dir / "events.jsonl"
    intercept_cmd = ["bear", "intercept", "--output", str(events_path), "--"] + build_command
    success, output, error, _ = run_subprocess(intercept_cmd)
    if not success:
        raise RuntimeError(f"{shlex.join(intercept_cmd)} failed:\n{output + error}")
    if not events_path.exists():
        raise RuntimeError(f"bear intercept succeeded but {events_path} was not written")

    # Parse events.jsonl once and write per-target outputs.
    db = BuildDatabase.from_events(events_path)
    db.write_outputs(output_dir)


def main():
    logging.basicConfig(level=logging.INFO)

    parser = argparse.ArgumentParser(
        prog="ideas.bear",
        description="Runs bear intercept and extracts per-target compile_commands + links files.",
        epilog="example: python -m ideas.bear --output-dir build-ninja -- cmake --build build-ninja --target all",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("."),
        metavar="DIR",
        help="Directory to write output files (default: current directory)",
    )
    parser.add_argument(
        "build_command",
        nargs=argparse.REMAINDER,
        help="Build command to pass to bear",
    )
    args = parser.parse_args()
    # build_command will include "--" so strip it if present
    if args.build_command and args.build_command[0] == "--":
        args.build_command.pop(0)
    if not args.build_command:
        parser.error("A build command must be provided")

    try:
        _main(args.build_command, args.output_dir)
    except Exception as e:
        logger.exception(e)
        sys.exit(1)


if __name__ == "__main__":
    main()
