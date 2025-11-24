#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))
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
TESTGEN_DIR ?= testgen.$(shell git rev-parse HEAD)## Directory to put IDEAS test generation
TESTGEN_ARGS ?= ## Args to pass to IDEAS test generation
RUSTFLAGS ?= -Awarnings## Flags to build Rust translation
VERBOSE ?= 0## Whether to output failed/partial projects in summaries

AFL_TAG = aflplusplus/aflplusplus:stable

# Pass these variables to IDEAS.mk
export MODEL BASE_URL TRANSLATION_DIR TESTGEN_DIR RUSTFLAGS

EXAMPLES ?= $(sort $(shell find ${EXAMPLES_DIR} -maxdepth 3 -name test_case -type d))## List of examples to run on
ifeq ($(EXAMPLES),)
$(warning No projects found in ${EXAMPLES_DIR}. You may need to re-run commands!)
endif


all: help ;

.PHONY: install
install: install-uv install-rust install-deno ## Install uv, Rust, and Deno

.PHONY: install-uv
install-uv:## Install uv@0.7.12
	curl -LsSf https://astral.sh/uv/0.7.12/install.sh | sh

.PHONY: install-rust
install-rust:## Install Rust@1.88.0
	curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- --default-toolchain 1.88.0

.PHONY: install-deno
install-deno:## Install Deno, which is required by dspy.PythonInterpreter()
	curl -fsSL https://deno.land/install.sh | sh

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
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) init


.PHONY: examples/cmake
examples/cmake:## CMake generate and build all examples
examples/cmake: $(subst /test_case,/cmake,${EXAMPLES}) ;
	@echo "# cmake"
	@echo "\`\`\`"
	@find ${EXAMPLES_DIR} -path "*/build-ninja/build.log" -size 0 -exec echo SUCCEEDED \; | uniq -c
	@find ${EXAMPLES_DIR} -path "*/build-ninja/build.log" -size +0 -exec echo FAILED \; | uniq -c
	@echo "\`\`\`"
examples/%/cmake:## CMake generate and build specific example
examples/%/cmake: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake


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
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) translate


.PHONY: examples/wrapper
examples/wrapper:## Generate C FFI wrappers for all examples
examples/wrapper: $(subst /test_case,/wrapper,${EXAMPLES})
examples/%/wrapper:## Generate C FFI wrappers for specific example
examples/%/wrapper: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) wrapper


.PHONY: examples/add_test_vectors
examples/add_test_vectors:## Build all translated examples
examples/add_test_vectors: $(subst /test_case,/add_test_vectors,${EXAMPLES})
examples/%/add_test_vectors:## Build specific translated example
examples/%/add_test_vectors: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
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
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
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
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) test


.PHONY: examples/repair
examples/repair:## Repair all examples
examples/repair: $(subst /test_case,/repair,${EXAMPLES})
examples/%/repair:## Repair specific example
examples/%/repair: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
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
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) update_tests

# clean
clean:
	rm -rf examples
	git checkout HEAD examples

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
	@echo "  make examples/test TRANSLATION_DIR=test_case ${GREY_COL}# Build and run tests on C examples ${RESET}"
