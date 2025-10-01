#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))
PIPELINE_DIR := lib/pipeline_automation
PIPELINE_TAG := ideas/$(shell git rev-list -1 HEAD -- ${PIPELINE_DIR})
EXAMPLES_DIR := examples
IDEAS_MAKEFILE := $(MAKEFILE_DIR)/IDEAS.mk

PROVIDER ?= hosted_vllm## Provider to use with DSPy/LiteLLM
MODEL ?= Qwen/Qwen3-Coder-30B-A3B-Instruct## Model to use to translate
REVISION ?= None## Revision of model to load in vLLM
HOST ?= localhost
PORT ?= 8000## Port to use for vLLM
BASE_URL ?= http://${HOST}:${PORT}/v1## Base URL of vLLM server
VLLM_ARGS ?= --tensor-parallel-size 8 --enable-expert-parallel --max-num-seqs 32 --max-model-len 128k## Args to pass to vllm serve
TRANSLATION_DIR ?= translation.$(shell git rev-parse HEAD)## Directory to put IDEAS translation
TRANSLATE_ARGS ?= ## Args to pass to IDEAS translation
RUSTFLAGS ?= -Awarnings## Flags to build Rust translation
VERBOSE ?= 0## Whether to output failed/partial projects in summaries

AFL_TAG = aflplusplus/aflplusplus:stable

# Pass these variables to IDEAS.mk
export MODEL BASE_URL TRANSLATION_DIR RUSTFLAGS

EXAMPLES ?= $(sort $(shell find ${EXAMPLES_DIR} -name test_case -type d))## List of examples to run on
ifeq ($(EXAMPLES),)
$(warning No projects found in ${EXAMPLES_DIR}. You may need to re-run commands!)
endif

all: help ;

# FIXME: We are using a hotfix to run `carge generate_lockfile` on the host.
.PHONY: build
build:## Build unsafety, idiomaticity, and c2rust Docker images
build: ${PIPELINE_DIR}/evaluate_unsafe_usage/unsafety.Dockerfile \
       ${PIPELINE_DIR}/idiomaticity/idiomaticity_measurements.Dockerfile \
       ${PIPELINE_DIR}/c2rust/install_c2rust_generate_lockfile_on_host.Dockerfile
	docker build -t ${PIPELINE_TAG}/unsafety \
                 -f ${PIPELINE_DIR}/evaluate_unsafe_usage/unsafety.Dockerfile \
                 ${PIPELINE_DIR}/evaluate_unsafe_usage/
	docker build -t ${PIPELINE_TAG}/idiomaticity \
                 -f ${PIPELINE_DIR}/idiomaticity/idiomaticity_measurements.Dockerfile \
                 ${PIPELINE_DIR}/idiomaticity/
	docker build -t ${PIPELINE_TAG}/c2rust \
                 -f ${PIPELINE_DIR}/c2rust/install_c2rust_generate_lockfile_on_host.Dockerfile \
                 ${PIPELINE_DIR}/c2rust/
	docker pull ${AFL_TAG}

# Comment out cargo generate-lockfile in the Docker image.
lib/pipeline_automation/c2rust/c2rust_commands_generate_lockfile_on_host.sh: lib/pipeline_automation/c2rust/c2rust_commands.sh
	sed 's/cargo generate-lockfile/#cargo generate-lockfile/g' $< > $@

# Copy the modified script to the Docker image.
lib/pipeline_automation/c2rust/install_c2rust_generate_lockfile_on_host.Dockerfile: lib/pipeline_automation/c2rust/install_c2rust.Dockerfile lib/pipeline_automation/c2rust/c2rust_commands_generate_lockfile_on_host.sh lib/pipeline_automation/c2rust/invoke_c2rust_generate_lockfile_on_host.py
	sed 's/c2rust_commands\.sh/c2rust_commands_generate_lockfile_on_host\.sh/g' $< > $@

# Run cargo generate-lockfile on the host after the Docker container exits.
lib/pipeline_automation/c2rust/invoke_c2rust_generate_lockfile_on_host.py: lib/pipeline_automation/c2rust/invoke_c2rust.py
	sed '$$a\  subprocess.check_call(["cargo", "generate-lockfile"], cwd=args.out)' $< > $@

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
	uv run vllm serve ${MODEL} --revision ${REVISION} --host ${HOST} --port ${PORT} --dtype auto ${VLLM_ARGS}

kill:## Kill all vLLM serves
	pkill -f "^uv run vllm serve" -u ${USER}

.PHONY: FORCE
FORCE:

.PHONY: examples/init
examples/init:## Initialize all examples
examples/init: $(subst /test_case,/init,${EXAMPLES}) ;
	@echo "# ${TRANSLATION_DIR}"
examples/%/init:## Initialize specific example
examples/%/init: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) init


.PHONY: examples/translate
examples/translate:## Translate all examples
examples/translate: $(subst /test_case,/translate,${EXAMPLES})
	@echo "# ${TRANSLATION_DIR}"
	@echo "\`\`\`"
	@echo "--- Translation Count ---"
	@find ${EXAMPLES_DIR} -path "*/${TRANSLATION_DIR}/translate.log" | wc -l
	@echo "\`\`\`"
examples/%/translate:## Translate specific example
examples/%/translate: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) translate

.PHONY: examples/add_test_vectors
examples/add_test_vectors:## Build all translated examples
examples/add_test_vectors: $(subst /test_case,/add_test_vectors,${EXAMPLES})
examples/%/add_test_vectors:## Build specific translated example
examples/%/add_test_vectors: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) add_test_vectors

.PHONY: examples/build
examples/build:## Build all translated examples
examples/build: $(subst /test_case,/build,${EXAMPLES})
	@echo "# ${TRANSLATION_DIR}"
	@echo "\`\`\`"
	@echo "--- Project Builds ---"
	@find ${EXAMPLES_DIR} -path "*/${TRANSLATION_DIR}/build.log" -size 0 -exec echo SUCCEEDED \; | uniq -c
	@find ${EXAMPLES_DIR} -path "*/${TRANSLATION_DIR}/build.log" -size +0 -exec echo FAILED \; | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES_DIR} -path "*/${TRANSLATION_DIR}/build.log" -size +0 | sort | sed -e "s/${TRANSLATION_DIR}.*//gi" | sed -e "s/^/    FAILED /gi"
	@echo ""
endif
	@echo "\`\`\`"
examples/%/build:## Build specific translated example
examples/%/build: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) build

.PHONY: examples/test
examples/test:## Test all translated examples
examples/test: $(subst /test_case,/test,${EXAMPLES})
	@echo "# ${TRANSLATION_DIR}"
	@echo "\`\`\`"
	@echo "--- Project Builds ---"
	@find ${EXAMPLES_DIR} -path "*/${TRANSLATION_DIR}/build.log" -size 0 -exec echo SUCCEEDED \; | uniq -c
	@find ${EXAMPLES_DIR} -path "*/${TRANSLATION_DIR}/build.log" -size +0 -exec echo FAILED \; | uniq -c
	@echo ""
ifneq (${VERBOSE},0)
	@find ${EXAMPLES_DIR} -path "*/${TRANSLATION_DIR}/build.log" -size +0 | sort | sed -e "s/${TRANSLATION_DIR}.*//gi" | sed -e "s/^/    FAILED /gi"
	@echo ""
endif
	@echo "--- Project Completion Count ---"
	@find ${EXAMPLES_DIR} -path '*/${TRANSLATION_DIR}/cargo_test.log' -exec ./scripts/test_log_stats.sh {} \; | cut -d" " -f1 | sort | uniq -c
	@echo ""
ifneq (${VERBOSE},0)
	@find ${EXAMPLES_DIR} -path '*/${TRANSLATION_DIR}/cargo_test.log' -exec ./scripts/test_log_stats.sh {} \; | egrep "PARTIAL" | sort | sed -e "s/${TRANSLATION_DIR}.*//gi" | sed -e 's/^/    /'
	@echo ""
	@find ${EXAMPLES_DIR} -path '*/${TRANSLATION_DIR}/cargo_test.log' -exec ./scripts/test_log_stats.sh {} \; | egrep "FAILED" | sort | sed -e "s/${TRANSLATION_DIR}.*//gi" | sed -e 's/^/    /'
	@echo ""
endif
	@echo "--- Aggregated Test Count ---"
	@find ${EXAMPLES_DIR} -path '*/${TRANSLATION_DIR}/cargo_test.log' | xargs cat | grep -aE "^test \S+ ... \S+$$" | cut -d" " -f4 | sort | uniq -c
	@echo "\`\`\`"
examples/%/test:## Test specific translated example
examples/%/test: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) test

.PHONY: examples/repair
examples/repair:## Repair all examples
examples/repair: $(subst /test_case,/repair,${EXAMPLES})
examples/%/repair:## Repair specific example
examples/%/repair: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) repair

.PHONY: examples/clean
examples/clean:## Clean all examples
examples/clean: $(subst /test_case,/clean,${EXAMPLES})
examples/%/clean:## Clean specific example
examples/%/clean: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) clean

# experiment across preprocessing strategies
PREPROC_STRATEGIES ?= clang clang-directive-filter clang-sys-filter tu tu-sys-filter c## List of preprocessing stragegies to use in experiment

.PRECIOUS: ${TRANSLATION_DIR}.preproc_strategy_%.log
${TRANSLATION_DIR}.preproc_strategy_%.log:
	$(MAKE) --no-print-directory examples/test TRANSLATION_DIR="${TRANSLATION_DIR}.preproc_strategy_$*" TRANSLATE_ARGS="${TRANSLATE_ARGS} preproc_strategy=$*" | tee -a $@

.PHONY: examples/experiment
examples/experiment: $(addsuffix .log,$(addprefix ${TRANSLATION_DIR}.preproc_strategy_,${PREPROC_STRATEGIES}))## Run all preprocessing strategies and print metrics
	@for PREPROC_STRATEGY in ${PREPROC_STRATEGIES} ; \
	do \
	  grep -A100 "# ${TRANSLATION_DIR}.preproc_strategy_$$PREPROC_STRATEGY" ${TRANSLATION_DIR}.preproc_strategy_$$PREPROC_STRATEGY.log ; \
	  echo "" ; \
	done

# update tests
.PHONY: examples/update_tests
examples/update_tests:## Update test cases to use TRANSLATION_DIR test cases for all examples
examples/update_tests: $(subst /test_case,/update_tests,${EXAMPLES})
examples/%/update_tests:## Update specific test cases to use TRANSLATION_DIR test cases
examples/%/update_tests: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) update_tests

# clean
clean:
	rm -rf ${PIPELINE_DIR} examples
	git checkout HEAD ${PIPELINE_DIR} examples

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
	@echo "  make examples/test TRANSLATION_DIR=c2rust ${GREY_COL}# Translate, build, and run tests on c2rust translated examples${RESET}"
	@echo "  make examples/test TRANSLATION_DIR=test_case ${GREY_COL}# Build and run tests on C examples ${RESET}"
