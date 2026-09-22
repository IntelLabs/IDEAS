#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import contextlib
import filecmp
import logging
import os
import secrets
import shutil
import socket
import sys
import tempfile
import threading
from collections.abc import Generator
from pathlib import Path

import tomlkit

from ideas.tools import Crate

VERIFIER_COMMAND_ENV = "IDEAS_TEST_VERIFIER"
VERIFIER_SOCKET_ENV = "IDEAS_TEST_VERIFIER_SOCKET"

_VERIFICATION_TESTS = ("collect", "io")
_ARTIFACT_DIRS = ("coverage_logs", "json", "sanitizer_logs")
_ignore_artifacts = shutil.ignore_patterns(".git", "target", *_ARTIFACT_DIRS)

logger = logging.getLogger("ideas.agents.verifiers")


class VerificationError(Exception):
    """A generated test is not portable."""


def _restore(path: Path, snapshot: tuple[bytes, int]) -> bool:
    try:
        if (
            not path.is_symlink()
            and (
                path.read_bytes(),
                path.stat().st_mode & 0o7777,
            )
            == snapshot
        ):
            return False
    except OSError:
        pass
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)
    path.write_bytes(snapshot[0])
    path.chmod(snapshot[1])
    return True


def get_fresh_copy(crate: Crate, destination: Path) -> Crate:
    random_name = f"crate-{secrets.token_hex(8)}".replace("-", "_")
    fresh_dir = destination / random_name
    shutil.copytree(crate.cargo_toml.parent, fresh_dir, ignore=_ignore_artifacts)

    manifest_path = fresh_dir / "Cargo.toml"
    manifest = tomlkit.loads(manifest_path.read_text())
    if (package := manifest.get("package")) is None:
        raise RuntimeError(f"Expected a package manifest at {manifest_path}!")
    package["name"] = random_name

    if (library := manifest.get("lib")) is not None:
        library["name"] = random_name
    if (binaries := manifest.get("bin")) is not None:
        if len(binaries) > 1:
            raise RuntimeError(
                f"Expected at most one binary in the manifest at {manifest_path}!"
            )
        binaries[0]["name"] = random_name
    manifest_path.write_text(tomlkit.dumps(manifest))

    if crate.main_src_path is not None and crate.lib_name is not None:
        main = fresh_dir / crate.main_src_path.relative_to(crate.cargo_toml.parent)
        main.write_text(
            main.read_text().replace(f"use {crate.lib_name}::*;", f"use {random_name}::*;", 1)
        )
    return Crate(manifest_path)


def verify_tests_are_portable(source: Crate, fresh: Crate, tests: list[str]) -> None:
    if not tests:
        raise VerificationError("No generated test was submitted for verification!")
    if unknown := [test for test in tests if test not in _VERIFICATION_TESTS]:
        raise VerificationError(f"Unknown generated test `{unknown[0]}`!")

    source_script = source.cargo_toml.parent / "instrument.sh"
    fresh_script = fresh.cargo_toml.parent / "instrument.sh"
    pristine_script = fresh_script.read_bytes(), fresh_script.stat().st_mode & 0o7777
    try:
        script_was_modified = _restore(source_script, pristine_script)
    except OSError as error:
        raise VerificationError(
            "The original `instrument.sh` could not be restored!"
        ) from error
    if script_was_modified:
        raise VerificationError(
            "The provided `instrument.sh` was modified and has been reverted."
        )

    source_dir, fresh_dir = source.cargo_toml.parent, fresh.cargo_toml.parent
    (fresh_dir / "tests").mkdir(parents=True, exist_ok=True)
    for test in tests:
        try:
            contents = (source_dir / "tests" / f"{test}.rs").read_text()
            contents = contents.replace(
                f"use {source.lib_name}::*;", f"use {fresh.lib_name}::*;", 1
            )
            (fresh_dir / "tests" / f"{test}.rs").write_text(contents)
        except OSError as error:
            raise VerificationError(
                f"Cannot copy the generated `{test}.rs` test to another crate!"
            ) from error

    for test in tests:
        passes, output, error, _ = fresh.cargo_test(test)
        if any([_restore(script, pristine_script) for script in (source_script, fresh_script)]):
            raise VerificationError(
                "Re-running tests in a fresh environment unexpectedly modified "
                "`instrument.sh` in-place; it was reverted!"
            )
        if not passes:
            raise VerificationError(
                f"The generated `{test}.rs` is not portable to a fresh crate copy "
                "after editing the top-level imports!\n" + output + error
            )


def _directory_difference(comparison: filecmp.dircmp, relative: Path = Path()) -> Path | None:
    changed = sorted(
        comparison.left_only
        + comparison.right_only
        + comparison.diff_files
        + comparison.common_funny
        + comparison.funny_files
    )
    if changed:
        return relative / changed[0]

    for name in comparison.common:
        left, right = Path(comparison.left) / name, Path(comparison.right) / name
        if (
            left.is_symlink() != right.is_symlink()
            or left.lstat().st_mode & 0o7777 != right.lstat().st_mode & 0o7777
            or left.is_symlink()
            and left.readlink() != right.readlink()
            or name in comparison.common_files
            and not filecmp.cmp(left, right, shallow=False)
        ):
            return relative / name
    for name, child in sorted(comparison.subdirs.items()):
        if difference := _directory_difference(child, relative / name):
            return difference
    return None


def verify_instrumentation(
    candidate_manifest: Path,
    workspace: Path,
    fresh_manifest: Path,
    tests: str | list[str],
) -> None:
    """Run instrumentation-loop checks in a temporary pristine workspace."""
    if isinstance(tests, str):
        tests = [tests]
    with tempfile.TemporaryDirectory(prefix="ideas-verifier-") as scratch:
        staged_root = Path(scratch) / "workspace"
        shutil.copytree(workspace, staged_root, ignore=_ignore_artifacts)
        fresh = Crate(staged_root / fresh_manifest)
        pristine_workspace = fresh.workspace_root
        try:
            verify_tests_are_portable(Crate(candidate_manifest), fresh, tests)
            shutil.rmtree(pristine_workspace / "target", ignore_errors=True)
            for name in _ARTIFACT_DIRS:
                shutil.rmtree(fresh.cargo_toml.parent / name, ignore_errors=True)

            relative = fresh.cargo_toml.parent.relative_to(pristine_workspace)
            for test in tests:
                pristine_test = workspace / relative / "tests" / f"{test}.rs"
                staged_test = fresh.cargo_toml.parent / "tests" / f"{test}.rs"
                if pristine_test.exists():
                    _restore(
                        staged_test,
                        (pristine_test.read_bytes(), pristine_test.stat().st_mode & 0o7777),
                    )
                else:
                    staged_test.unlink(missing_ok=True)
                    with contextlib.suppress(OSError):
                        staged_test.parent.rmdir()

            comparison = filecmp.dircmp(workspace, staged_root)
            if difference := _directory_difference(comparison):
                raise VerificationError(
                    "Running tests in a fresh crate changed an unexpected workspace path: "
                    f"`{difference}`!"
                )
        except VerificationError as error:
            message = str(error).replace(scratch, "<pristine-workspace>")
            raise VerificationError(message) from None


def run_cli(arguments: list[str]) -> tuple[int, str | None]:
    if len(arguments) != 2:
        return 2, "Usage: verifier <socket> <collect|io>"
    try:
        with socket.socket(socket.AF_UNIX) as connection:
            connection.connect(arguments[0])
            connection.sendall((arguments[1] + "\n").encode())
            response = connection.makefile("rb").read().decode()
        status, _, message = response.partition("\n")
        return int(status), message or None
    except (OSError, UnicodeError, ValueError):
        return 1, "The test verifier failed unexpectedly!"


@contextlib.contextmanager
def verification_service(candidate: Crate, crate: Crate) -> Generator[None]:
    """Verify script runs and retain the latest successful generated-test pair."""
    workspace = crate.workspace_root.resolve()
    fresh_manifest = crate.cargo_toml.relative_to(workspace)
    latest_pair: dict[str, tuple[bytes, int]] = {}
    completed = False

    with (
        tempfile.TemporaryDirectory(prefix="ideas-verifier-") as temporary,
        socket.socket(socket.AF_UNIX) as listener,
    ):
        socket_path = Path(temporary) / "verifier.sock"
        listener.bind(str(socket_path))
        listener.listen()

        def serve() -> None:
            successful: dict[str, tuple[bytes, int]] = {}
            while True:
                try:
                    connection, _ = listener.accept()
                except OSError:
                    return
                with connection:
                    test = connection.recv(64).decode(errors="replace").strip()
                    try:
                        verify_instrumentation(
                            candidate.cargo_toml, workspace, fresh_manifest, [test]
                        )
                        path = candidate.cargo_toml.parent / "tests" / f"{test}.rs"
                        successful[test] = path.read_bytes(), path.stat().st_mode & 0o7777
                        if all(name in successful for name in _VERIFICATION_TESTS):
                            latest_pair.update(successful)
                        status, message = 0, None
                    except VerificationError as error:
                        status, message = 1, str(error)
                    except Exception:
                        status, message = 1, "The test verifier failed unexpectedly!"
                    connection.sendall(f"{status}\n{message or ''}".encode())

        server = threading.Thread(target=serve, daemon=True)
        server.start()

        values = {
            VERIFIER_COMMAND_ENV: sys.executable,
            VERIFIER_SOCKET_ENV: str(socket_path),
        }
        previous = {name: os.environ.get(name) for name in values}
        os.environ.update(values)
        try:
            yield
            completed = True
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            with contextlib.suppress(OSError):
                listener.shutdown(socket.SHUT_RDWR)
            server.join()

    if not completed:
        return
    try:
        verify_instrumentation(
            candidate.cargo_toml, workspace, fresh_manifest, list(_VERIFICATION_TESTS)
        )
    except VerificationError as error:
        message = str(error).splitlines()[0]
        if len(latest_pair) == len(_VERIFICATION_TESTS):
            for test, (contents, mode) in latest_pair.items():
                path = candidate.cargo_toml.parent / "tests" / f"{test}.rs"
                _restore(path, (contents, mode))
            logger.warning(
                "Final verification failed (%s); restored the last verified pair", message
            )
        else:
            logger.warning("Final verification failed and no verified pair exists: %s", message)


def main() -> None:
    status, message = run_cli(sys.argv[1:])
    if message is not None:
        print(message, file=sys.stderr)
    raise SystemExit(status)


if __name__ == "__main__":
    main()
