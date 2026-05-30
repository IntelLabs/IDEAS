#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
from pathlib import Path

from ideas.cmake import _normalize_isystem


def test_normalize_isystem_command_space_separated(tmp_path: Path):
    """Replaces '-isystem /path' with '-I /path' in command strings."""
    db = [
        {"directory": "/build", "command": "cc -isystem /usr/include -c foo.c", "file": "foo.c"}
    ]
    p = tmp_path / "compile_commands.json"
    p.write_text(json.dumps(db))

    _normalize_isystem(p)

    result = json.loads(p.read_text())
    assert result[0]["command"] == "cc -I /usr/include -c foo.c"


def test_normalize_isystem_command_no_space(tmp_path: Path):
    """Replaces '-isystem/path' with '-I/path' in command strings."""
    db = [
        {"directory": "/build", "command": "cc -isystem/usr/include -c foo.c", "file": "foo.c"}
    ]
    p = tmp_path / "compile_commands.json"
    p.write_text(json.dumps(db))

    _normalize_isystem(p)

    result = json.loads(p.read_text())
    assert result[0]["command"] == "cc -I/usr/include -c foo.c"


def test_normalize_isystem_arguments_space_separated(tmp_path: Path):
    """Replaces '-isystem' followed by path in arguments array."""
    db = [
        {
            "directory": "/build",
            "arguments": ["cc", "-isystem", "/usr/include", "-c", "foo.c"],
            "file": "foo.c",
        }
    ]
    p = tmp_path / "compile_commands.json"
    p.write_text(json.dumps(db))

    _normalize_isystem(p)

    result = json.loads(p.read_text())
    assert result[0]["arguments"] == ["cc", "-I", "/usr/include", "-c", "foo.c"]


def test_normalize_isystem_arguments_joined(tmp_path: Path):
    """Replaces '-isystem/path' in arguments array."""
    db = [
        {
            "directory": "/build",
            "arguments": ["cc", "-isystem/usr/include", "-c", "foo.c"],
            "file": "foo.c",
        }
    ]
    p = tmp_path / "compile_commands.json"
    p.write_text(json.dumps(db))

    _normalize_isystem(p)

    result = json.loads(p.read_text())
    assert result[0]["arguments"] == ["cc", "-I/usr/include", "-c", "foo.c"]


def test_normalize_isystem_multiple_entries(tmp_path: Path):
    """Handles multiple entries and multiple -isystem flags per entry."""
    db = [
        {
            "directory": "/build",
            "command": "cc -isystem /a -isystem /b -c foo.c",
            "file": "foo.c",
        },
        {"directory": "/build", "command": "cc -I/c -c bar.c", "file": "bar.c"},
    ]
    p = tmp_path / "compile_commands.json"
    p.write_text(json.dumps(db))

    _normalize_isystem(p)

    result = json.loads(p.read_text())
    assert result[0]["command"] == "cc -I /a -I /b -c foo.c"
    assert result[1]["command"] == "cc -I/c -c bar.c"


def test_normalize_isystem_no_isystem(tmp_path: Path):
    """No-op when there are no -isystem flags."""
    db = [{"directory": "/build", "command": "cc -I/usr/include -c foo.c", "file": "foo.c"}]
    p = tmp_path / "compile_commands.json"
    p.write_text(json.dumps(db))

    _normalize_isystem(p)

    result = json.loads(p.read_text())
    assert result[0]["command"] == "cc -I/usr/include -c foo.c"


def test_normalize_isystem_missing_file(tmp_path: Path):
    """No-op when compile_commands.json does not exist."""
    p = tmp_path / "compile_commands.json"
    _normalize_isystem(p)  # should not raise
    assert not p.exists()
