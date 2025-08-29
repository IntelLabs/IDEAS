#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

import hydra
import logging
from pathlib import Path
from hydra.types import RunMode
from hydra.core.config_store import ConfigStore
from hydra.core.hydra_config import HydraConfig
from dataclasses import dataclass, field
from omegaconf import MISSING

from ideas import ModelConfig, GenerateConfig, AlgorithmConfig
from ideas import translate_file


@dataclass
class IdeasConfig:
    hydra: dict = field(
        default_factory=lambda: {
            "mode": RunMode.RUN,  # https://github.com/facebookresearch/hydra/issues/2262
            "hydra_logging": {"handlers": {"console": {"stream": "ext://sys.stderr"}}},
            "job_logging": {
                "formatters": {
                    "json_code": {"()": "ideas.JSONFormatter"},
                    "rust_code": {"format": "%(rust)s"},
                },
                "filters": {"translation": {"()": "ideas.CodePairFilter"}},
                "handlers": {
                    "console": {"stream": "ext://sys.stderr"},
                    "file": {"filename": "${hydra.runtime.output_dir}/${hydra.job.name}.log"},
                    "json_file": {
                        "class": "logging.FileHandler",
                        "filename": "${hydra.runtime.output_dir}/translation.jsonl",
                        "filters": ["translation"],
                        "formatter": "json_code",
                    },
                    "rust_file": {
                        "class": "logging.FileHandler",
                        "filename": "${hydra.runtime.output_dir}/translation.rs",
                        "filters": ["translation"],
                        "formatter": "rust_code",
                    },
                    "rust_console": {
                        "class": "logging.StreamHandler",
                        "stream": "ext://sys.stdout",
                        "filters": ["translation"],
                        "formatter": "rust_code",
                    },
                },
                "loggers": {
                    "ideas.translate": {
                        "level": "INFO",
                        "handlers": ["json_file", "rust_file", "rust_console"],
                    }
                },
            },
        }
    )
    filename: Path = MISSING
    model: ModelConfig = field(default_factory=ModelConfig)
    generate: GenerateConfig = field(default_factory=GenerateConfig)
    algorithm: AlgorithmConfig = field(default_factory=AlgorithmConfig)

    def __post_init__(self):
        if self.filename.suffix not in (".i"):
            raise ValueError(
                "Input file must be a pre-processed C source file with .i extension!"
            )


cs = ConfigStore.instance()
cs.store(name="ideas", node=IdeasConfig)


@hydra.main(version_base=None, config_name="ideas")
def main(cfg: IdeasConfig) -> None:
    logging.getLogger("ideas").info(f"Saving results to {HydraConfig.get().runtime.output_dir}")
    translate_file(
        cfg.filename,
        model_cfg=cfg.model,
        generate_cfg=cfg.generate,
        algorithm_cfg=cfg.algorithm,
    )


if __name__ == "__main__":
    main()
