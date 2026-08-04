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
        executable_basename = Path(arguments[0]).name
        # Match exact names and versioned variants e.g. clang-21, gcc-13
        if not any(
            executable_basename == d or executable_basename.startswith(d + "-")
            for d in _LINKERS
        ):
            return False
        # Must not be a compile-only step
        if "-c" in arguments:
            return False
        # Must have at least one object file input to distinguish from non-linker calls
        return any(arg.endswith(".o") for arg in arguments[1:])

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
    def target(self) -> TargetName | None:
        for i, arg in enumerate(self.arguments):
            if arg == "-o" and i + 1 < len(self.arguments):
                name = Path(self.arguments[i + 1]).name
                # Strip version suffix from shared libraries: libfoo.so.1.2.3 -> libfoo.so
                return re.sub(r"\.so(\.\d+)+$", ".so", name)
        return None

    @cached_property
    def output_path(self) -> Path | None:
        for i, arg in enumerate(self.arguments):
            if arg == "-o" and i + 1 < len(self.arguments):
                p = Path(self.arguments[i + 1])
                return (p if p.is_absolute() else self.working_dir / p).resolve()
        return None

    def _input_args(self) -> list[str]:
        result = []
        it = iter(self.arguments[1:])  # skip argv[0]
        for arg in it:
            if arg == "-o":
                next(it, None)  # consume and discard the output path
            elif not arg.startswith("-"):
                result.append(arg)
        return result

    @cached_property
    def object_files(self) -> list[Path]:
        objects: list[Path] = []
        for arg in self._input_args():
            p = Path(arg)
            if p.suffix == ".o":
                resolved = p if p.is_absolute() else self.working_dir / p
                objects.append(resolved.resolve())
        return objects

    @cached_property
    def linked_binary_inputs(self) -> list[Path]:
        binaries: list[Path] = []
        for arg in self._input_args():
            p = Path(arg)
            if (".so" in p.name and p.suffix != ".o") or p.suffix == ".a":
                resolved = p if p.is_absolute() else self.working_dir / p
                binaries.append(resolved.resolve())
        return binaries

    @cached_property
    def link_libs(self) -> list[str]:
        return [arg[2:] for arg in self.arguments if arg.startswith("-l") and arg[2:]]


_SOURCE_EXTS = {".c", ".cpp", ".cxx", ".cc", ".C", ".s", ".S", ".m"}
_COMPILERS = {"gcc", "clang", "cc", "g++", "c++", "clang++"}
_LINKERS = {
    "gcc",
    "clang",
    "cc",
    "g++",
    "c++",
    "clang++",
    "ld",
    "ld.bfd",
    "ld.lld",
    "ld.gold",
    "lld",
}
# CMake injects these dependency-tracking flags into every compile command.
# They cause libclang to try writing .d files at relative paths that don't
# exist during analysis, which fails TranslationUnit parsing.
_DEP_FLAGS = frozenset({"-MD", "-MMD", "-MP", "-MG"})
_DEP_FLAGS_WITH_ARG = frozenset({"-MF", "-MT", "-MQ"})


@dataclass
class TargetOutputs:
    entries: list[CompileCommand]
    link_libs: list[str]


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
        binary_source_map: dict[Path, list[Path]],
    ) -> list[CompileCommand]:
        entries: list[CompileCommand] = []
        seen_sources: set[Path] = set()
        seen_binaries: set[Path] = set()

        def _collect(binary: Path) -> None:
            if binary in seen_binaries:
                return
            seen_binaries.add(binary)
            for inp in binary_source_map.get(binary, []):
                if (entry := obj_map.get(inp)) is not None:
                    # inp is a .o — add its compilation entry (dedup by source path)
                    if entry.source not in seen_sources:
                        seen_sources.add(entry.source)
                        entries.append(entry)
                elif inp in binary_source_map:
                    # inp is a .so/.a built in this project — recurse
                    _collect(inp)
                elif inp.suffix == ".o":
                    # No compile command for this object file. System CRT objects
                    # (crti.o, crtbeginS.o, etc.) under /usr are expected — log at
                    # debug. Any other gap is unexpected and logged as a warning.
                    if inp.is_relative_to(Path("/usr")):
                        logger.debug(
                            "Skipping system object file: %s (linked into %s)",
                            inp,
                            link_cmd.target,
                        )
                    else:
                        logger.warning(
                            "No compile command found for object file: %s (linked into %s)",
                            inp,
                            link_cmd.target,
                        )
                else:
                    # External .so/.a not built in this project — skip but log so
                    # the user can verify it is intentionally external.
                    logger.debug("Skipping external binary input: %s", inp)

        if link_cmd.output_path:
            _collect(link_cmd.output_path)
        return entries

    def resolve_targets(self) -> dict[TargetName, TargetOutputs]:
        obj_map: dict[Path, CompileCommand] = {cmd.output: cmd for cmd in self.compile_commands}
        binary_source_map: dict[Path, list[Path]] = {}
        for lc in self.link_commands:
            if lc.output_path is None:
                continue
            inputs = lc.object_files + lc.linked_binary_inputs
            binary_source_map[lc.output_path] = inputs
            # Also index by the unversioned name (libfoo.so.1.2.3 → libfoo.so) so that
            # consumers referencing the symlink name are resolved transitively.
            normalized = lc.output_path.parent / re.sub(
                r"\.so(\.\d+)+$", ".so", lc.output_path.name
            )
            if normalized != lc.output_path:
                binary_source_map[normalized] = inputs
        result: dict[TargetName, TargetOutputs] = {}
        for link_cmd in self.link_commands:
            assert link_cmd.target is not None
            entries = self._collect_transitive_entries(link_cmd, obj_map, binary_source_map)
            result[link_cmd.target] = TargetOutputs(
                entries=entries, link_libs=link_cmd.link_libs
            )
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
            link_entries += [{"lib": lib} for lib in outputs.link_libs]
            (target_dir / "links.json").write_text(
                json.dumps({"entries": link_entries}, indent=2)
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
