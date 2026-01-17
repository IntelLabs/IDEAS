#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import json
import pickle
import shutil
import logging
import tempfile
from pathlib import Path
from collections import defaultdict, OrderedDict
from dataclasses import dataclass, field

import dspy
import hydra
from omegaconf import MISSING
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig
from dspy.teleprompt.gepa.gepa_utils import DSPyTrace, ScoreWithFeedback

from ideas import model, ModelConfig, GenerateConfig, tools
from ideas.translate_symbol import SymbolTranslatorSignature

logger = logging.getLogger("ideas.learn.translate")


@dataclass
class TrainConfig:
    student_examples: Path = MISSING
    teacher_examples: Path = MISSING

    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)

    reflect_model: ModelConfig = field(default_factory=ModelConfig)
    reflect_generate: GenerateConfig = field(
        default_factory=lambda: GenerateConfig(
            temperature=1.0,
            max_new_tokens=32000,
        )
    )


cs = ConfigStore.instance()
cs.store(name="learn.translate", node=TrainConfig)


def metric(
    gold: dspy.Example,
    pred: dspy.Prediction,
    trace: DSPyTrace | None = None,
    pred_name: str | None = None,
    pred_trace: DSPyTrace | None = None,
) -> float | ScoreWithFeedback:
    with tempfile.TemporaryDirectory() as tmpdir:
        shutil.copytree(gold.crate_path, tmpdir, dirs_exist_ok=True)
        cargo_toml = Path(tmpdir) / "Cargo.toml"
        crate = tools.Crate(cargo_toml=cargo_toml)

        # Make sure translation returned something
        pred_translation = ""
        if "translation" in pred and pred.translation is not None:
            pred_translation = pred.translation.code

        # Write predicted translation
        rust_srcs = []
        for name, translation in gold.crate_translation.items():
            if name != gold.symbol_name:
                rust_srcs.append(translation)
            else:
                rust_srcs.append(pred_translation)

        rust_src = "\n\n".join(rust_srcs)
        # Make libraries wrapper-aware
        if not crate.is_bin:
            rust_src += "\n\npub mod wrapper;\n"

        crate.rust_src_path.write_text(rust_src)

        # Attempt to build and run all tests
        success, _ = tools.run_subprocess(["cargo", "test", f"--manifest-path={cargo_toml}"])

    if not success:
        return ScoreWithFeedback(
            score=0.0,
            feedback=f"The translation failed!\n\nA correct Rust translation is:\n```rust\n{gold.translation}\n```",
        )
    else:
        return 1.0


def get_crate_and_data_paths(cargo_toml: str) -> tuple[Path, Path]:
    cargo_toml_path = Path(cargo_toml).resolve()
    if cargo_toml_path.is_dir():
        cargo_toml_path = cargo_toml_path / "Cargo.toml"

    crate = tools.Crate(cargo_toml=cargo_toml_path)
    jsonl_path = crate.rust_src_path.with_suffix(".jsonl")

    return jsonl_path, cargo_toml_path


def split_examples(
    student: Path, teacher: Path
) -> tuple[list[dspy.Example], list[dspy.Example]]:
    train_examples, val_examples = [], []
    for student_cargo_toml, teacher_cargo_toml in zip(
        student.read_text().splitlines(), teacher.read_text().splitlines()
    ):
        student_jsonl, _ = get_crate_and_data_paths(student_cargo_toml)
        teacher_jsonl, teacher_cargo_toml_path = get_crate_and_data_paths(teacher_cargo_toml)

        # Find out where the student fails
        student_success = defaultdict(bool)
        for jsonl in student_jsonl.read_text().splitlines():
            student_translation = json.loads(jsonl)
            if student_translation["success"]:
                student_success[student_translation["symbol_name"]] = True

        # Accumulate all successful teacher translations for the crate
        crate_translation = OrderedDict()
        for jsonl in teacher_jsonl.read_text().splitlines():
            teacher_translation = json.loads(jsonl)
            if teacher_translation["success"]:
                crate_translation[teacher_translation["symbol_name"]] = teacher_translation[
                    "translation"
                ]

        # Create examples using trajectories of successful translations
        for jsonl_teacher in teacher_jsonl.read_text().splitlines():
            teacher_translation = json.loads(jsonl_teacher)
            example = dspy.Example(
                crate_path=teacher_cargo_toml_path.parent,
                crate_translation=crate_translation,
                **teacher_translation,
            ).with_inputs(
                "reference_code",
                "snippet",
                "dependent_code",
                "prior_translation",
                "feedback",
            )

            # Use failed translations for validation
            if (
                student_success[teacher_translation["symbol_name"]]
                and teacher_translation["success"]
            ):
                train_examples.append(example)
            else:
                val_examples.append(example)
    return train_examples, val_examples


@hydra.main(version_base=None, config_name="learn.translate")
def main(cfg: TrainConfig) -> None:
    logging.getLogger("dspy").propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    output_dir = Path(HydraConfig.get().runtime.output_dir)
    logger.info(f"Saving results to {output_dir}")

    model.configure(cfg.model, cfg.generate)
    reflection_lm = model.get_lm(cfg.reflect_model, cfg.reflect_generate)

    # Construct train/val sets
    trainset, valset = split_examples(cfg.student_examples, cfg.teacher_examples)

    if len(trainset) == 0:
        raise ValueError("Learning requires at least one correct symbol translation!")
    if len(valset) == 0:
        raise ValueError("All symbols are already perfectly translated!")

    gepa = dspy.GEPA(
        metric=metric,
        num_threads=min(len(valset), 32),
        log_dir=str(output_dir),
        auto="light",
        reflection_lm=reflection_lm,
        reflection_minibatch_size=min(len(trainset), 8),
        skip_perfect_score=False,
    )

    program = dspy.ChainOfThought(SymbolTranslatorSignature)
    optimized_program = gepa.compile(
        program,
        trainset=trainset,
        valset=valset,
    )
    print(optimized_program)
    optimized_program.save(output_dir / "optimized_program.json")
    optimized_program.save(output_dir / "optimized_program.pkl")
    optimized_program.save(output_dir / "optimized_program", save_program=True)
    f = open(output_dir / "optimized_program_history.pkl", "wb")
    pickle.dump(optimized_program.history, f)
    f.close()


if __name__ == "__main__":
    main()
