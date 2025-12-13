#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import io
import json
import logging
from pathlib import Path
from difflib import unified_diff
from graphlib import TopologicalSorter
from contextlib import redirect_stdout
from dataclasses import dataclass, field
from collections import OrderedDict, defaultdict, deque

import dspy
import hydra
from omegaconf import MISSING
from clang.cindex import TranslationUnit
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig

from ideas import model, ModelConfig, GenerateConfig, tools
from ideas import get_info_from_cargo_toml, extract_info_c
from .tools import Crate
from .init import get_symbols_and_dependencies
from .ast import get_cursor_code

logger = logging.getLogger("ideas.translate")


@dataclass
class TranslateConfig:
    filename: Path = MISSING
    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)

    translator: str = "ChainOfThought"
    max_iters: int = 5


cs = ConfigStore.instance()
cs.store(name="translate", node=TranslateConfig)


class Translator(dspy.Module):
    def __init__(
        self,
        translator: type[dspy.Module],
        max_iters: int = 5,
    ):
        super().__init__()
        self.translator = translator
        self.max_iters = max_iters

    def forward(self, filename: Path, crate: Crate) -> dspy.Prediction:
        # Get global symbol table
        tu = TranslationUnit.from_source(filename)
        asts = [extract_info_c(tu)]
        symbols, dependencies = get_symbols_and_dependencies(asts)
        references = transpose_graph(dependencies)

        # Assemble C sources in topological order
        sources: dict[str, str] = OrderedDict()
        for symbol_name in TopologicalSorter(dependencies).static_order():
            # Ignore tag definitions and function declarations
            if symbol_name not in symbols:
                logger.warning(f"Skipping `{symbol_name}` ...")
                continue
            sources[symbol_name] = get_cursor_code(symbols[symbol_name].cursor)

        # Translate symbol by symbol
        translations: dict[str, str] = OrderedDict()
        for symbol_name, symbol_code in sources.items():
            dep_names = [
                name for name in bfs(symbol_name, references, max_depth=1) if name in sources
            ]

            # Gather reference and dependent code in order of translations and sources, respectively
            ref_translations = "\n\n".join(translations.values())
            dep_sources = "\n\n".join(
                [source for name, source in sources.items() if name in dep_names]
            )

            logger.info(f"Translating `{symbol_name}` ...")
            logger.debug(f"```c\n{symbol_code}\n```")

            # FIXME: Pass a Symbol here instead of symbol_code and is_snippet_main. Similarly,
            #        dep_sources should probably be a list[Symbol] too.
            pred = self.translate_with_feedback(
                ref_translations,
                symbol_code,
                dep_sources,
                crate,
                max_iters=self.max_iters,
                is_snippet_main=symbol_name == "c:@F@main",
            )
            # pred = dspy.Prediction(translation=dspy.Code(code=""))

            # Logging
            logger.info(f"Translated `{symbol_name}` ...")
            logger.debug(f"```rust\n{pred.translation.code}\n```")

            # Update state
            translations[symbol_name] = pred.translation.code
            with crate.rust_src_path.with_suffix(".jsonl").open("a") as f:
                for prior_translation, feedback in zip(pred.prior_translations, pred.feedbacks):
                    jsonl = json.dumps(
                        {
                            "name": symbol_name,
                            "reference_names": list(translations.keys()),
                            "reference_code": ref_translations,
                            "snippet": symbol_code,
                            "dependent_names": dep_names,
                            "dependent_code": dep_sources,
                            "prior_translation": prior_translation,
                            "feedback": feedback,
                            "translation": pred.translation.code,
                            "success": pred.success,
                        }
                    )
                    f.write(jsonl + "\n")

        translation = "\n\n".join(translations.values())
        return dspy.Prediction(translation=translation)

    class TranslateSignature(dspy.Signature):
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

    def translate(
        self,
        reference_code: str,
        snippet: str,
        dependent_code: str,
        *,
        prior_translation: str = "",
        feedback: str = "",
    ) -> dspy.Prediction:
        translate = self.translator(Translator.TranslateSignature)

        pred = translate(
            reference_code=reference_code,
            snippet=snippet,
            dependent_code=dependent_code,
            prior_translation=prior_translation,
            feedback=feedback,
        )
        # FIXME: Add rustfmt and FeedbackException?
        return pred

    # FIXME: convert to using symbol
    def translate_with_feedback(
        self,
        reference_code: str,
        snippet: str,
        dependent_code: str,
        crate: Crate,
        *,
        max_iters: int = 0,
        is_snippet_main: bool = False,
    ) -> dspy.Prediction:
        pred = self.translate(reference_code, snippet, dependent_code)
        success, prior_translations, feedbacks = False, [""], [""]
        for _ in range(max_iters):
            rust_src = ""
            if len(reference_code) > 0:
                rust_src += reference_code + "\n\n"
            rust_src += pred.translation.code + "\n\n"
            if crate.is_bin and not is_snippet_main:
                # Work around E0601 error
                rust_src += 'fn main() {\n    println!("Hello, world!");\n}\n'

            crate.rust_src_path.write_text(rust_src)
            env = os.environ.copy()
            env["RUSTFLAGS"] = (env.get("RUSTFLAGS", "") + " -D unsafe-code").strip()
            success, feedback = tools.run_subprocess(
                [
                    "cargo",
                    "build",
                    "--quiet",
                    "--color=never",
                    f"--manifest-path={crate.cargo_toml}",
                ],
                env=env,
            )
            if success:
                break
            logger.debug(
                f"Feedback\n```rust\n{reference_code}\n{pred.translation.code}\n```\n\n# Feedback\n{feedback}\n\n# reasoning\n{pred.reasoning}"
            )

            feedbacks.append(feedback)
            prior_translations.append(pred.translation.code)
            pred = self.translate(
                reference_code,
                snippet,
                dependent_code,
                prior_translation=pred.translation.code,
                feedback=feedback,
            )
        else:
            logger.warning(
                f"Translation failed to build after {max_iters} feedback iterations!"
            )
        pred["feedbacks"] = feedbacks
        pred["prior_translations"] = prior_translations
        pred["success"] = success
        return pred

    def get_history(self, n: int = 1, clear: bool = False) -> str:
        f = io.StringIO()
        with redirect_stdout(f):
            self.inspect_history(n=n, clear=clear)
        return f.getvalue().strip()

    def inspect_history(self, n: int = 1, clear: bool = True):
        super().inspect_history(n)
        if clear:
            self.history = []


def diff(old: str, new: str):
    diff = "\n".join(
        unified_diff(
            old.splitlines(), new.splitlines(), lineterm="", fromfile="old", tofile="new"
        )
    )
    return diff


def rustc(translation: dspy.Code["Rust"] | str) -> str:  # noqa: F821
    "Compiles the translation using rustc and returns any errors."
    if isinstance(translation, dspy.Code):
        code = translation.code
    else:
        code = translation
    success, output = tools.run_subprocess(
        ["rustc", "-A", "warnings", "--crate-type", "lib", "--edition", "2024", "-"],
        input=code,
    )
    return "" if success else output


def rustfmt(translation: dspy.Code["Rust"] | str) -> str:  # noqa: F821
    "Formats the Rust code using rustfmt."
    if isinstance(translation, dspy.Code):
        code = translation.code
    else:
        code = translation
    _, formatted_code = tools.run_subprocess(
        ["rustfmt", "--edition", "2024", "--color", "never"], input=code
    )
    return formatted_code


def transpose_graph(graph: dict[str, list[str]]) -> dict[str, list[str]]:
    transpose: dict[str, list[str]] = defaultdict(list)
    for node, neighbors in graph.items():
        for neighbor in neighbors:
            transpose[neighbor].append(node)
    return dict(transpose)


def bfs(node: str, graph: dict[str, list[str]], max_depth: int = -1) -> list[str]:
    nodes = [node]
    queue = deque()
    queue.append((node, 0))
    while queue:
        curr_node, level = queue.popleft()
        for neighbor in graph.get(curr_node, []):
            # ignore visited or too deep nodes
            if neighbor in nodes or (max_depth >= 0 and level + 1 > max_depth):
                continue
            nodes.append(neighbor)
            queue.append((neighbor, level + 1))
    # ignore initial node
    return nodes[1:]


@hydra.main(version_base=None, config_name="translate")
def main(cfg: TranslateConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger.info(f"Saving results to {output_dir}")
    crate = get_info_from_cargo_toml(output_dir / "Cargo.toml")

    model.configure(cfg.model, cfg.generate)
    if cfg.translator == "ChainOfThought":
        translator = dspy.ChainOfThought
    elif cfg.translator == "Predict":
        translator = dspy.Predict
    else:
        raise ValueError(f"Unknown translator: {cfg.translator}!")

    agent = Translator(translator, cfg.max_iters)
    pred = agent(cfg.filename, crate)
    translation = pred.translation
    # Write translation and history to disk
    crate.rust_src_path.parent.mkdir(exist_ok=True, parents=True)
    crate.rust_src_path.write_text(translation)
    crate.rust_src_path.with_suffix(".history").write_text(agent.get_history(n=100000))
    logger.info(f"Saved translation to {crate.rust_src_path}")


if __name__ == "__main__":
    main()
