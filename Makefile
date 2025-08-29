#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))
IDEAS_MAKEFILE := $(MAKEFILE_DIR)/IDEAS.mk

PROVIDER ?= hosted_vllm## Provider to use with DSPy/LiteLLM
MODEL ?= Qwen/Qwen3-Coder-30B-A3B-Instruct## Model to use to translate
REVISION ?= None## Revision of model to load in vLLM
PORT ?= 8000## Port to use for vLLM
BASE_URL ?= http://localhost:${PORT}/v1## Base URL of vLLM server
VLLM_ARGS ?= --tensor-parallel-size 8 --enable-expert-parallel --max-num-seqs 48 --max-model-len 60k## Args to pass to vllm serve
TRANSLATION_DIR ?= translation.$(shell git rev-parse HEAD)## Directory to put IDEAS translation
TRANSLATE_ARGS ?=## Args to pass to IDEAS translation
RUSTFLAGS ?= -Awarnings## Flags to build Rust translation

# Pass these variables to IDEAS.mk
export MODEL BASE_URL TRANSLATION_DIR RUSTFLAGS

EXAMPLES := $(sort $(shell find examples -name project -type d))
ifeq ($(EXAMPLES),)
$(warning No projects found in examples. You may need to re-run commands!)
endif

all: help ;

.PHONY: install
install: install-uv install-rust## Install uv and Rust

.PHONY: install-uv
install-uv:## Install uv@0.7.12
	curl -LsSf https://astral.sh/uv/0.7.12/install.sh | sh

.PHONY: install-rust
install-rust:## Install Rust@1.87.0
	curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- --default-toolchain 1.87.0

.PHONY: test
test:## Run pytest
	uv run pytest

.PHONY: serve
serve:## Start vLLM server
	uv run vllm serve ${MODEL} --revision ${REVISION} --host 0.0.0.0 --port ${PORT} --dtype auto ${VLLM_ARGS}

kill:## Kill all vLLM serves
	pkill -f "^uv run vllm serve" -u ${USER}

.PHONY: FORCE
FORCE:

.PHONY: examples/translate
examples/translate:## Translate all examples
examples/translate: $(subst /project,/translate,${EXAMPLES})
	@echo "Cumulative Translations"
	find examples -path "*/${TRANSLATION_DIR}/translate.log" -size 0 | wc -l
	find examples -path "*/${TRANSLATION_DIR}/translate.log" -size +0 | wc -l
examples/%/translate:## Translate specific example
examples/%/translate: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) translate

.PHONY: examples/build
examples/build:## Build all translated examples
examples/build: $(subst /project,/build,${EXAMPLES})
	@echo "# {TRANSLATION_DIR}"
	@echo "\`\`\`"
	@echo "--- Cumulative Builds ---"
	find examples -path "*/${TRANSLATION_DIR}/build.log" -size 0 | wc -l
	find examples -path "*/${TRANSLATION_DIR}/build.log" -size +0 | wc -l
	@echo "\`\`\`"
examples/%/build:## Build specific translated example
examples/%/build: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) build

.PHONY: examples/test
examples/test:## Test all translated examples
examples/test: $(subst /project,/test,${EXAMPLES})
	@echo "# ${TRANSLATION_DIR}"
	@echo "\`\`\`"
	@echo "--- Cumulative Builds ---"
	find examples -path "*/${TRANSLATION_DIR}/build.log" -size 0 | wc -l
	find examples -path "*/${TRANSLATION_DIR}/build.log" -size +0 | wc -l
	@echo "--- Cumulative Tests ---"
	@find examples -path '*/${TRANSLATION_DIR}/test.log' | xargs cat | grep -E "^(PASS|FAIL)" | cut -d" " -f1 | sort | uniq -c
	@echo "--- Per-Project Completion ---"
	@find examples -path '*/${TRANSLATION_DIR}/test.log' -exec ./scripts/test_log_stats.sh {} \; | cut -d" " -f1 | sort | uniq -c
	@echo "\`\`\`"
examples/%/test:## Test specific translated example
examples/%/test: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) test

.PHONY: examples/clean
examples/clean:## Clean all examples
examples/clean: $(subst /project,/clean,${EXAMPLES})
examples/%/clean:## Clean specific example
examples/%/clean: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) clean

# help
RESET := \033[0;0m
CYAN_COL := \033[0;36m
YELLOW_COL:= \033[0;33m
GREY_COL := \033[1;30m
help:
	@echo "Usage:"
	@echo "  make ${CYAN_COL}[target] ${YELLOW_COL}[variables]${RESET}"
	@echo ""
	@echo "Targets:"
	@grep -E "^[a-zA-Z/_%%]+:.*?##.*$$" ${MAKEFILE_LIST} \
     | awk 'BEGIN { FS=":.*##" } ; \
          { printf "  ${CYAN_COL}%-30s${RESET}%s\n", $$1, $$2 }'
	@echo ""
	@echo "Variables:"
	@grep -E "^[a-zA-Z_]+ [:?!+]?=.*?##.*$$" ${MAKEFILE_LIST} \
     | awk 'BEGIN { FS=" [:?!+]?= |##" } ; \
          { printf "  ${YELLOW_COL}%-30s${RESET}%s ${GREY_COL}(default: %s)${RESET}\n", $$1, $$3, $$2}'
	@echo ""
	@echo "Examples:"
	@echo "  make examples/test TRANSLATION_DIR=project ${GREY_COL}# Build and run tests on C examples ${RESET}"
