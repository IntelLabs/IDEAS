#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import io
import re
import sys
import logging
from pathlib import Path

import dspy
from clang.cindex import TranslationUnit, CursorKind

from .ast import extract_info_c, get_cursor_prettyprinted
from .tools import extract_rust, check_rust
from .ltu import build_unit
from .cover import CoVeR

logger = logging.getLogger("ideas.agent")


class Translation(dspy.Signature):
    """Translate the C code to idiomatic, memory-safe Rust."""

    input_code: dspy.Code["C"] = dspy.InputField(desc="The input source code")  # noqa: F821
    output_code: dspy.Code["Rust"] = dspy.OutputField(  # noqa: F821
        desc="The idiomatic and memory-safe source code that is functionally equivalent to the input source code"
    )


class PreProcessing(dspy.Module):
    def __init__(self, preproc_strategy: str):
        super().__init__()

        self.preproc_strategy = preproc_strategy

    def forward(
        self, c_code: str, c_full_code: str, tu: TranslationUnit
    ) -> dict[str, list[str]]:
        output_code = []
        # Use Clang to analyze the pre-processed C code
        ast_info = extract_info_c(tu)

        match self.preproc_strategy:
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

        return {"input_code": output_code}


class TranslateAgent(dspy.Module):
    def __init__(
        self,
        preproc_strategy: str,
        translator: str,
        max_iters: int,
        use_raw_fixer_output: bool,
    ):
        super().__init__()

        self.preprocessor = PreProcessing(preproc_strategy)
        self.translate_signature = Translation

        success_message: str = "Completed!"

        def compile_rust(rust_code: str) -> str:
            rust_code = extract_rust(rust_code)
            success, compile_messages = check_rust(
                rust_code, flags=["-A", "dead_code", "--crate-type", "lib"]
            )
            if success:
                compile_messages = success_message
            return compile_messages

        self.compile_tool = dspy.Tool(
            func=compile_rust,
            name="compile_rust",
            desc=f'Compiles the Rust code standalone and checks for errors. Returns "{success_message}" when compilation succeeds.',
        )

        match translator:
            case "ReAct":
                self.translator = dspy.ReAct(
                    self.translate_signature, tools=[self.compile_tool], max_iters=max_iters
                )

            case "CoVeR":
                self.translator = CoVeR(
                    self.translate_signature,
                    tools=[self.compile_tool],
                    success=success_message,
                    max_iters=max_iters,
                    use_raw_fixer_output=use_raw_fixer_output,
                )

            case "Predict":
                self.translator = dspy.Predict(self.translate_signature)

            case _:
                raise ValueError(f"Invalid translator: {translator}")

    def forward(
        self,
        input_code_path: Path,
        input_code: str,
        full_code_path: Path,
        full_code: str,
        tu: TranslationUnit,
    ) -> dspy.Prediction:
        logger.info(f"Translating {input_code_path} ...")

        translation_inputs: dspy.Prediction = self.preprocessor(input_code, full_code, tu)

        output_code = []
        for input_code in translation_inputs["input_code"]:
            translation: dspy.Prediction = self.translator(input_code=input_code)
            # dspy.Code.code gets the code str.
            snippet = translation.output_code.code

            output_code.append(snippet)

        # Concatenate inputs and outputs
        input_code = "\n\n// next input\n".join(translation_inputs["input_code"])
        output_code = "\n\n// next output\n".join(output_code)

        # Save dspy history to string
        history = io.StringIO()
        sys.stdout = history
        dspy.inspect_history(100)
        sys.stdout = sys.__stdout__

        return dspy.Prediction(
            input_code_path=input_code_path,
            full_code_path=full_code_path,
            input_code=input_code,
            output_code=output_code,
            history=history.getvalue(),
        )
