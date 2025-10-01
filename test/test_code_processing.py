#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import pytest
from pathlib import Path

from ideas import agents
from clang.cindex import TranslationUnit


@pytest.fixture
def fixtures_dir() -> Path:
    return Path(__file__).parent / "fixtures" / "code_preprocessing"


@pytest.fixture
def input_code(fixtures_dir: Path) -> str:
    return (fixtures_dir / "input.c.i").read_text()


@pytest.fixture
def expected_code(fixtures_dir: Path) -> str:
    return (fixtures_dir / "expected.c.i").read_text()


def test_system_filter(input_code: str, expected_code: str):
    c_code = ""
    c_full_code = input_code
    tu = TranslationUnit.from_source("file.c", unsaved_files=[("file.c", c_full_code)])

    preprocessor = agents.PreProcessing(preproc_strategy="clang-sys-filter")
    c_translation_inputs = preprocessor(c_code, c_full_code, tu)

    assert "input_code" in c_translation_inputs
    assert len(c_translation_inputs["input_code"]) == 1
    assert c_translation_inputs["input_code"][0].strip() == expected_code.strip()
