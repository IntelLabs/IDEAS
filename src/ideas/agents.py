#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import re
import dspy
import logging
from dataclasses import dataclass

from clang.cindex import TranslationUnit, CursorKind
from hydra.core.config_store import ConfigStore
from dspy.adapters import Tool
from dspy.predict import Predict, ReAct

from .ast import extract_info_c, get_cursor_prettyprinted
from .tools import extract_rust, check_rust
from .ltu import build_unit
from .cover import CoVeR

logger = logging.getLogger("ideas.agent")


@dataclass
class AlgorithmConfig:
    preproc_strategy: str = "clang"
    request_fenced_code: bool = True
    max_iters: int = 5

    translator: str = "ReAct"
    use_raw_fixer_output: bool = False

    def __post_init__(self):
        if self.preproc_strategy not in [
            "clang",
            "clang-directive-filter",
            "clang-sys-filter",
            "tu",
            "tu-sys-filter",
            "c",
            "ltu-max",
            "ltu-min",
        ]:
            raise ValueError(f"Invalid C preprocessor strategy: {self.preproc_strategy}")

        if self.translator not in ["Predict", "ReAct", "CoVeR"]:
            raise ValueError(f"Invalid translator: {self.translator}")


cs = ConfigStore.instance()
cs.store(name="algorithm", node=AlgorithmConfig)


class Translation(dspy.Signature):
    """Translate the C code to idiomatic, memory-safe Rust."""

    c_code: str = dspy.InputField(desc="The C code to translate")
    rust_code: str = dspy.OutputField(
        desc="Idiomatic, memory-safe Rust code that is functionally equivalent to the original C code"
    )


class FencedTranslation(Translation):
    """Translate the C code to idiomatic, memory-safe Rust and output a single fenced code block."""

    rust_code: str = dspy.OutputField(
        desc="A single fenced code block of idiomatic, memory-safe Rust code that is functionally equivalent to the original C code"
    )


class PreProcessing(dspy.Module):
    def __init__(self, cfg: AlgorithmConfig):
        super().__init__()

        self.cfg = cfg

    def forward(
        self, c_code: str, c_full_code: str, tu: TranslationUnit
    ) -> dict[str, list[str]]:
        output_code = []
        # Use Clang to analyze the pre-processed C code
        ast_info = extract_info_c(tu)

        match self.cfg.preproc_strategy:
            case "clang":
                output_code = [c_full_code]

            case "clang-directive-filter":
                lines = c_full_code.splitlines()
                non_directive_lines = []
                for line in lines:
                    if re.match(r"^# .*", line):
                        continue
                    non_directive_lines.append(line)
                output_code = ["\n".join(non_directive_lines)]

            case "clang-sys-filter":
                lines = c_full_code.splitlines()
                non_usr_lines, keep = [], True
                for line in lines:
                    # Ignore until next comment
                    if re.match(r'^# [0-9]+ "/usr', line):
                        keep = False
                        continue

                    # Start retaining, except for the comment itself
                    if re.match(r'^# [0-9]+ "(?!/usr)', line):
                        keep = True
                        continue

                    if keep:
                        non_usr_lines.append(line)
                output_code = ["\n".join(non_usr_lines)]

            case "tu":
                output_code = ""
                for child in tu.cursor.get_children():
                    # Filter out non-first column cursors since there should be another cursor at column 1
                    if child.extent.start.column != 1:
                        continue

                    output_code += get_cursor_prettyprinted(child)

                    # Non-function definitions require statement terminations
                    if child.kind != CursorKind.FUNCTION_DECL or not child.is_definition():  # type: ignore
                        output_code += ";"

                    output_code += "\n"
                output_code = [output_code]

            case "tu-sys-filter":
                output_code = ""
                for child in tu.cursor.get_children():
                    # Filter out non-first column cursors since there should be another cursor at column 1
                    if child.extent.start.column != 1:
                        continue
                    # Filter out system library expansions
                    if child.location.file.name.startswith("/usr"):
                        continue

                    output_code += get_cursor_prettyprinted(child)

                    # Non-function definitions require statement terminations
                    if child.kind != CursorKind.FUNCTION_DECL or not child.is_definition():  # type: ignore
                        output_code += ";"

                    output_code += "\n"
                output_code = [output_code]

            case "c":
                output_code = [c_code]

            case "ltu-max":
                output_units = build_unit(ast_info, type="functional_maximal")
                output_code = [str(unit) for unit in output_units]

            case "ltu-min":
                output_units = build_unit(ast_info, type="functional_minimal")
                output_code = [str(unit) for unit in output_units]

        return {"c_code": output_code}


class Agent(dspy.Module):
    def __init__(self, cfg: AlgorithmConfig):
        super().__init__()

        self.cfg = cfg
        self.preprocessor = PreProcessing(self.cfg)
        self.translate_signature = FencedTranslation if cfg.request_fenced_code else Translation
        self.translator = Predict(self.translate_signature)

    def forward(self, c_code: str, c_full_code: str, tu: TranslationUnit) -> dict[str, str]:
        c_translation_inputs: dict[str, list[str]] = self.preprocessor(c_code, c_full_code, tu)

        rust_code = []
        for c_input in c_translation_inputs["c_code"]:
            translation: dspy.Prediction = self.translator(c_code=c_input)
            # FIXME: There has to be a way to get dspy to parse rust_code
            snippet = extract_rust(translation.rust_code)

            rust_code.append(snippet)

        # Concatenate C inputs and Rust outputs
        c_code = "\n\n// next input\n".join(c_translation_inputs["c_code"])
        rust_code = "\n\n// next output\n".join(rust_code)

        return {
            "c_code": c_code,
            "rust_code": rust_code,
        }


class AgentWithFeedback(Agent):
    def __init__(self, cfg: AlgorithmConfig):
        super().__init__(cfg)

        success_message: str = "Success!"

        def compile_rust(rust_code: str) -> str:
            rust_code = extract_rust(rust_code)
            success, compile_messages = check_rust(rust_code)
            if success:
                compile_messages = success_message
            return compile_messages

        self.compile_tool = Tool(
            func=compile_rust,
            name="compile_rust",
            desc=f'Compiles the Rust code standalone and checks for errors. Returns "{success_message}" when compilation succeeds.',
        )

        match cfg.translator:
            case "ReAct":
                self.translator = ReAct(
                    self.translate_signature, tools=[self.compile_tool], max_iters=cfg.max_iters
                )

            case "CoVeR":
                self.translator = CoVeR(
                    self.translate_signature,
                    tools=[self.compile_tool],
                    max_iters=cfg.max_iters,
                    use_raw_fixer_output=cfg.use_raw_fixer_output,
                )

            case _:
                raise ValueError(f"Invalid translator: {cfg.translator}")


def from_config(cfg: AlgorithmConfig) -> dspy.Module:
    if cfg.translator == "Predict":
        return Predict(cfg)

    if cfg.translator in ["ReAct", "CoVeR"]:
        return AgentWithFeedback(cfg)

    raise ValueError(f"Invalid translator: {cfg.translator}.")
