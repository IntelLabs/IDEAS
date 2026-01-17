#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

from .logging import JSONFormatter, CodePairFilter
from .ast import create_translation_unit, extract_info_c, TreeResult
from .model import ModelConfig, GenerateConfig
from .translate_recurrent import RecurrentTranslator
from .translate_symbol import SymbolTranslator
from clang.cindex import Config

__version__ = "2025.10"

__all__ = [
    "create_translation_unit",
    "extract_info_c",
    "TreeResult",
    "ModelConfig",
    "GenerateConfig",
    "JSONFormatter",
    "CodePairFilter",
    "RecurrentTranslator",
    "SymbolTranslator",
]

# NOTE: .so is *nix specific
Config.set_library_file("libclang-21.so")
