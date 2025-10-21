#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import os
import logging
from pathlib import Path
from dataclasses import dataclass, field
from contextlib import chdir

import dspy
import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig
from clang.cindex import CompilationDatabase, TranslationUnit

from ideas import model, ModelConfig, GenerateConfig
from ideas import TranslateAgent, tools


@dataclass
class TranslateConfig:
    filename: Path = MISSING
    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)

    preproc_strategy: str = "clang"
    max_iters: int = 5

    translator: str = "CoVeR"
    use_raw_fixer_output: bool = True

    batched: bool = False


cs = ConfigStore.instance()
cs.store(name="translate", node=TranslateConfig)


@hydra.main(version_base=None, config_name="translate")
def translate(cfg: TranslateConfig) -> None:
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger = logging.getLogger("ideas.translate")
    logger.info(f"Saving results to {output_dir}")

    model.configure(cfg.model, cfg.generate)

    if cfg.filename.name == "compile_commands.json":
        logger.info(f"Parsing {cfg.filename} ...")

        # Create TranslationUnit from original .c filename in compile commands database
        db = CompilationDatabase.fromDirectory(cfg.filename.parent)
        cmds = db.getAllCompileCommands()
        tus = [TranslationUnit.from_source(None, args=list(cmd.arguments)) for cmd in cmds]

        # Construct intermediate .c.i filename
        c_paths = [Path(cmd.filename) for cmd in cmds]
        c_i_paths = [c_path.with_suffix(".c.i") for c_path in c_paths]

        # Read C code from disk and generate intermediate files using cmd
        c_codes = [path.read_text() for path in c_paths]
        c_i_codes = [generate_intermediate_file(list(cmd.arguments)) for cmd in cmds]

    elif cfg.filename.suffix == ".i":
        # Create TranslationUnit from intermediate .c.i filename
        tus = [TranslationUnit.from_source(cfg.filename)]

        # Construct original .c filename
        c_paths = [cfg.filename.parent / cfg.filename.stem]
        c_i_paths = [cfg.filename]

        # Read code from disk
        c_codes = [path.read_text() for path in c_paths]
        c_i_codes = [path.read_text() for path in c_i_paths]

    else:
        raise ValueError(
            "filename must be a pre-processed C source file with .i extension or compile_commands.json"
        )

    # Find common path amongst input paths and construct examples to pass to agent
    common_dir = Path(os.path.commonpath(c_paths))
    if not common_dir.is_dir():
        common_dir = common_dir.parent
    examples = [
        dspy.Example(
            input_code_path=c_path.relative_to(common_dir),
            input_code=c_code,
            full_code_path=c_i_path.relative_to(common_dir),
            full_code=c_i_code,
            tu=tu,
        ).with_inputs("input_code_path", "input_code", "full_code_path", "full_code", "tu")
        for c_path, c_code, c_i_path, c_i_code, tu in zip(
            c_paths, c_codes, c_i_paths, c_i_codes, tus
        )
    ]

    # Execute agent in parallel by changing cwd to common dir first
    agent = TranslateAgent(
        cfg.preproc_strategy,
        cfg.translator,
        cfg.max_iters,
        cfg.use_raw_fixer_output,
    )
    with chdir(common_dir):
        if cfg.batched:
            translations = agent.batch(
                examples,
                num_threads=len(examples),
                disable_progress_bar=len(examples) == 1,
                provide_traceback=True,
            )
        else:
            translations = [agent(**example.inputs()) for example in examples]

    if len(translations) != len(c_paths):
        logger.warning("A translation is missing!")

    for translation in translations:
        assert isinstance(translation, dspy.Prediction)
        filename = translation["input_code_path"]
        logger.info(f"Translated {filename} ...")

        # Write rust translation to disk
        rs_translation_path = output_dir / "src" / filename.with_suffix(".rs")
        rs_translation_path.parent.mkdir(parents=True, exist_ok=True)
        rs_translation_path.write_text(translation["output_code"])

        prompt_path = output_dir / "src" / filename.with_suffix(".translate_prompt")
        prompt_path.parent.mkdir(parents=True, exist_ok=True)
        prompt_path.write_text(translation["input_code"])

        history_path = output_dir / "src" / filename.with_suffix(".translate_history")
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(translation["history"])

    logger.info(f"Saved results to {output_dir}")


def generate_intermediate_file(cmd: list[str]):
    if "-o" in cmd:
        o_idx = cmd.index("-o")
        cmd.pop(o_idx)  # -o
        cmd.pop(o_idx)  # filename
    cmd += ["-E"]
    ret, out = tools.run_subprocess(cmd)
    if not ret:
        raise ValueError("Failed to produce intermediate file using: {' '.join(cmd)}\n{out}")
    return out


if __name__ == "__main__":
    translate()
