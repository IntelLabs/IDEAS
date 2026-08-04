#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from .ast import create_translation_unit, extract_info_c, TreeResult
from .model import ModelConfig, GenerateConfig
from .translate_recurrent import RecurrentTranslator
from .translate_snippet import SnippetTranslator
from .wrapper import WrapperGenerator
from clang.cindex import Config

__all__ = [
    "create_translation_unit",
    "extract_info_c",
    "TreeResult",
    "ModelConfig",
    "GenerateConfig",
    "RecurrentTranslator",
    "SnippetTranslator",
    "WrapperGenerator",
]

# NOTE: .so is *nix specific
Config.set_library_file("libclang-21.so")
