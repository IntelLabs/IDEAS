#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
import logging

import dspy

from .tools import Crate
from .utils import Symbol
from .ast import get_cursor_code


logger = logging.getLogger("ideas.translate_symbol")


class SymbolTranslatorSignature(dspy.Signature):
    """
    Generate an idiomatic, memory-safe Rust translation of the snippet.
    The reference_code contains Rust code that should be used by the translation.
    The snippet contains a single C definition to translate to idiomatic, memory-safe Rust.
    The dependent_code contains C code that uses the C snippet.
    Reason about the dependent_code to understand any special memory management or complex ownership requirements a safe and idiomatic translation may need to take into account.
    Ensure the translation of the snippet does not use any unsafe constructs!
    Do not refactor the reference_code in the translation!
    Do not translate dependent_code to Rust in the translation!
    Do not define any implementations (`impl`) in the translation!
    Always assume all C integer arithmetic operations on the underlying value are intended to have wrapping semantics, and thus any translation should use Rust's wrapping arithmetic functions like `wrapping_add`, `wrapping_shr`, etc..
    Analyze all bitwise operations carefully, especially rotations.
    For all bitwise operations, including those that may appear to swap bits for bytes, implement the behavior exactly as written in the C code, without making assumptions about intent.
    Use the `cargo build` feedback about the prior_translation, if provided, when generating the Rust translation.
    """

    reference_code: dspy.Code["Rust"] = dspy.InputField()  # noqa: F821
    snippet: dspy.Code["C"] = dspy.InputField()  # noqa: F821
    dependent_code: dspy.Code["C"] = dspy.InputField()  # noqa: F821
    prior_translation: dspy.Code["Rust"] = dspy.InputField()  # noqa: F821
    feedback: str = dspy.InputField()
    translation: dspy.Code["Rust"] = dspy.OutputField()  # noqa: F821


class SymbolTranslator(dspy.Module):
    def __init__(
        self,
        translator: type[dspy.Module],
        crate: Crate,
        max_iters: int = 5,
        dump_jsonl: bool = True,
    ):
        super().__init__()
        self.translate = translator(SymbolTranslatorSignature)
        self.crate = crate
        self.max_iters = max_iters
        self.dump_jsonl = dump_jsonl

    # FIXME: Convert reference_code to list[Symbol]
    def forward(
        self,
        reference_code: str,
        symbol: Symbol,
        dependent_symbols: list[Symbol],
        prior_translation: str = "",
        feedback: str = "",
    ) -> dspy.Prediction:
        snippet = get_cursor_code(symbol.cursor)
        dependent_code = "\n\n".join([get_cursor_code(s.cursor) for s in dependent_symbols])

        pred = dspy.Prediction()
        for i in range(max(self.max_iters, 1)):
            # Predict symbol translation
            pred = self.translate(
                reference_code=reference_code,
                snippet=snippet,
                dependent_code=dependent_code,
                prior_translation=prior_translation,
                feedback=feedback,
            )

            # Combine reference Rust code with predicted symbol translation to see if it builds
            rust_src = ""
            if len(reference_code) > 0:
                rust_src += reference_code + "\n\n"
            rust_src += pred.translation.code + "\n\n"
            if self.crate.is_bin and symbol.name != "c:@F@main":
                # Work around E0601 error
                rust_src += 'fn main() {\n    println!("Hello, world!");\n}\n'
            self.crate.rust_src_path.write_text(rust_src)
            self.crate.add(self.crate.rust_src_path)
            # FIXME: Add rustfmt and FeedbackException?
            builds, feedback = self.crate.cargo_build()
            pred["success"] = builds
            pred["feedback"] = feedback

            # Write intermediate translation to disk
            if self.dump_jsonl:
                with self.crate.rust_src_path.with_suffix(".jsonl").open("a") as f:
                    jsonl = json.dumps(
                        {
                            "symbol_name": symbol.name,
                            "reference_code": reference_code,
                            "snippet": snippet,
                            "dependent_code": dependent_code,
                            "prior_translation": prior_translation,
                            "feedback": pred.feedback,
                            "translation": pred.translation.code,
                            "success": pred.success,
                        }
                    )
                    f.write(jsonl + "\n")

            # Exit early if we build
            if pred.success:
                self.crate.commit(
                    f"Translated symbol `{symbol.name}`\n\n# Reasoning\n{pred.reasoning}"
                )
                break
            self.crate.commit(
                f"Failed to translate symbol `{symbol.name}` ({i + 1}/{self.max_iters})!\n\n# Reasoning\n{pred.reasoning}\n\n# Feedback\n{pred.feedback}"
            )
            prior_translation = pred.translation.code
        return pred
