#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


import pytest
from pathlib import Path

from ideas.ast_rust import validate_changes


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "templating"


@pytest.fixture
def template(fixtures_dir: Path) -> str:
    return (fixtures_dir / "template.rs").read_text()


@pytest.fixture
def modified_valid(fixtures_dir: Path) -> str:
    return (fixtures_dir / "modified_valid.rs").read_text()


@pytest.fixture
def modified_invalid(fixtures_dir: Path) -> str:
    return (fixtures_dir / "modified_invalid.rs").read_text()


def test_modified_valid(template: str, modified_valid: str):
    feedback = validate_changes(modified_valid, template)
    assert not feedback


def test_modified_invalid(template: str, modified_invalid: str):
    feedback = validate_changes(modified_invalid, template)
    assert feedback
    assert list(feedback.keys()) == ["top_level_changes", "signature_changes"]
