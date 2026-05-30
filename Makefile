#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))
EXAMPLES_DIR := examples
IDEAS_MAKEFILE := $(MAKEFILE_DIR)/IDEAS.mk
AGENTS_MAKEFILE := $(MAKEFILE_DIR)/AGENTS.mk

PROVIDER ?= openrouter## Provider to use with DSPy/LiteLLM
MODEL ?= anthropic/claude-sonnet-4.6## Model to use to translate
REVISION ?= None## Revision of model to load in vLLM
HOST ?= localhost
PORT ?= 8000## Port to use for vLLM
BASE_URL ?= http://${HOST}:${PORT}/v1## Base URL of vLLM server
VLLM_VERSION ?= 0.17.1
VLLM_ARGS ?= --tensor-parallel-size 8 --enable-expert-parallel --max-num-seqs 16 --max-model-len 128k## Args to pass to vllm serve
TRANSLATION_DIR ?= translation.$(shell git rev-parse HEAD)## Directory to put IDEAS translation
TRANSLATE_ARGS ?= ## Args to pass to IDEAS translation
RUSTFLAGS ?= -Awarnings## Flags to build Rust translation
VERBOSE ?= 0## Whether to output failed/partial projects in summaries
VCS ?= git## Whether to use version control during translation. Options: ['git', 'none']
CC ?= clang## C compiler to use for translation and building
EVALUATION_TEST ?= test_cases## Evaluation test directory/name to run; defaults to `test_cases`

# Pass these variables to other Makefiles
export PROVIDER MODEL BASE_URL TRANSLATION_DIR RUSTFLAGS CC EVALUATION_TEST

EXAMPLES ?= $(sort $(patsubst %/test_case,%,$(shell find ${EXAMPLES_DIR} -maxdepth 3 -name test_case -type d)))## List of examples to run on
ifeq ($(EXAMPLES),)
$(warning No projects found in ${EXAMPLES_DIR}. You may need to re-run commands!)
endif


all: help ;

.PHONY: docker/build
docker/build:## Build translation Docker image
docker/build: docker/docker_build.log

.PRECIOUS: docker/docker_build.log
docker/docker_build.log: docker/ideas.Dockerfile
	rm -rf docker/.venv
	cd docker && \
	docker build --build-arg USER_UID=$(shell id -u) \
                 --build-arg USER_GID=$(shell id -g) \
                 -f ideas.Dockerfile -t ideas-$(shell id -u) . && \
	docker images --quiet ideas:latest 2>&1 | tee $(notdir $@)

.PHONY: docker
docker:## Mount translation Docker image
docker: docker/docker_build.log
	mkdir -p docker/.venv
	docker run -it --rm -v ".:/home/user/IDEAS" \
                    -v "./docker/.venv:/home/user/IDEAS/.venv" \
                    -e OPENROUTER_API_KEY \
                    ideas-$(shell id -u) bash


.PHONY: install
install: install-uv install-rust ## Install uv and Rust

.PHONY: install-uv
install-uv:## Install uv@0.11.13
	curl -LsSf https://astral.sh/uv/0.11.13/install.sh | sh

.PHONY: install-rust
install-rust:## Install Rust@1.88.0 and tools
	curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- --default-toolchain 1.88.0
	rustup component add rustfmt
	rustup component add llvm-tools-preview --toolchain 1.88.0-x86_64-unknown-linux-gnu
	cargo install bindgen-cli --version 0.72.1
	cargo install cargo-llvm-cov --version 0.8.6
	cargo install cargo-nextest --version 0.9.114 --locked

.PHONY: install-clang
install-clang:## Install Clang-21, must be sudo
	wget https://apt.llvm.org/llvm.sh
	chmod +x llvm.sh
	-./llvm.sh 21 all
	rm ./llvm.sh

.PHONY: install-sys-deps
install-sys-deps:## Install system dependencies, must be sudo
	apt install libpcre3-dev

.PHONY: serve
serve:## Start vLLM server
	uv run --no-project --python 3.11 --with vllm==${VLLM_VERSION} vllm serve ${MODEL} --revision ${REVISION} --host ${HOST} --port ${PORT} --dtype auto ${VLLM_ARGS}

kill:## Kill all vLLM servers
	pkill -f "^uv run --no-project --python 3.11 --with vllm==${VLLM_VERSION} vllm serve" -u ${USER}

.PHONY: FORCE
FORCE:

.PHONY: examples
examples:## Print out examples
examples: $(addsuffix /print,${EXAMPLES}) ;
examples/%/print: FORCE
	@if [ -d "$(@D)" ]; then echo "$(@D)"; fi

.PHONY: examples/init
examples/init:## Initialize all examples
examples/init: $(addsuffix /init,${EXAMPLES}) ;
	@echo "# ${TRANSLATION_DIR}"
examples/%/init:## Initialize specific example
examples/%/init: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) init


.PHONY: examples/cmake
examples/cmake:## CMake generate and build all examples
examples/cmake: $(addsuffix /cmake,${EXAMPLES}) ;
	@echo "# cmake"
	@echo "\`\`\`"
	@find ${EXAMPLES} -path "*/build-ninja/build.log" -size 0 -exec echo SUCCEEDED \; | uniq -c
	@find ${EXAMPLES} -path "*/build-ninja/build.log" -size +0 -exec echo FAILED \; | uniq -c
	@echo "\`\`\`"
examples/%/cmake:## CMake generate and build specific example
examples/%/cmake: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake


.PHONY: examples/testgen_agent
examples/testgen_agent:## Generate test vectors for all targets in all C examples with an agent
examples/testgen_agent: $(addsuffix /testgen_agent,${EXAMPLES})
examples/%/testgen_agent:## Generate test vectors for all targets in a specific C example with an agent
examples/%/testgen_agent: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
	-@$(MAKE) -j1 -f $(AGENTS_MAKEFILE) -C $(@D) testgen_agent


.PHONY: examples/translate
examples/translate:## Translate all examples
examples/translate: $(addsuffix /translate,${EXAMPLES})
	@echo "# ${TRANSLATION_DIR}"
	@echo "\`\`\`"
	@echo "--- Translation Count ---"
	@find ${EXAMPLES} -path "*/${TRANSLATION_DIR}/translate.log" | wc -l
	@echo "\`\`\`"
examples/%/translate:## Translate specific example
examples/%/translate: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) translate


.PHONY: examples/build
examples/build:## Build all translated examples
examples/build: $(addsuffix /build,${EXAMPLES})
	@echo "# ${TRANSLATION_DIR}"
	@echo "\`\`\`"
	@echo "--- Project Builds ---"
	@find ${EXAMPLES} -path "*/${TRANSLATION_DIR}/build.log" -size 0 -exec echo SUCCEEDED \; | uniq -c
	@find ${EXAMPLES} -path "*/${TRANSLATION_DIR}/build.log" -size +0 -exec echo FAILED \; | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -path "*/${TRANSLATION_DIR}/build.log" -size +0 | sort | sed -e "s/${TRANSLATION_DIR}.*//gi" | sed -e "s/^/    FAILED /gi"
	@echo ""
endif
	@echo "\`\`\`"
examples/%/build:## Build specific translated example
examples/%/build: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) build


.PHONY: examples/test
examples/test:## Test all translated examples
examples/test: $(addsuffix /test,${EXAMPLES})
	@echo "# ${TRANSLATION_DIR}"
	@echo "\`\`\`"
	@echo "--- Project Builds ---"
	@find ${EXAMPLES} -path "*/${TRANSLATION_DIR}/build.log" -size 0 -exec echo SUCCEEDED \; | uniq -c
	@find ${EXAMPLES} -path "*/${TRANSLATION_DIR}/build.log" -size +0 -exec echo FAILED \; | uniq -c
	@echo ""
ifneq (${VERBOSE},0)
	@find ${EXAMPLES} -path "*/${TRANSLATION_DIR}/build.log" -size +0 | sort | sed -e "s/${TRANSLATION_DIR}.*//gi" | sed -e "s/^/    FAILED /gi"
	@echo ""
endif
	@echo "--- Project Completion Count ---"
	@find ${EXAMPLES} -path '*/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log' -exec ./scripts/test_log_stats.sh {} \; | cut -d" " -f1 | sort | uniq -c
	@echo ""
ifneq (${VERBOSE},0)
	@find ${EXAMPLES} -path '*/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log' -exec ./scripts/test_log_stats.sh {} \; | egrep "PARTIAL" | sort | sed -e "s/${TRANSLATION_DIR}.*//gi" | sed -e 's/^/    /'
	@echo ""
	@find ${EXAMPLES} -path '*/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log' -exec ./scripts/test_log_stats.sh {} \; | egrep "MISSING" | sort | sed -e "s/${TRANSLATION_DIR}.*//gi" | sed -e 's/^/    /'
	@echo ""
	@find ${EXAMPLES} -path '*/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log' -exec ./scripts/test_log_stats.sh {} \; | egrep "FAILED" | sort | sed -e "s/${TRANSLATION_DIR}.*//gi" | sed -e 's/^/    /'
	@echo ""
endif
	@echo "--- Aggregated Test Count ---"
	@find ${EXAMPLES} -path '*/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log' | xargs cat | grep -aE "^test \S+ ... \S+$$" | cut -d" " -f4 | sort | uniq -c
	@echo "\`\`\`"
examples/%/test:## Test specific translated example
examples/%/test: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) cmake
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) test


.PHONY: examples/clean
examples/clean:## Clean all examples
examples/clean: $(addsuffix /clean,${EXAMPLES})
examples/%/clean:## Clean specific example
examples/%/clean: FORCE
	-@$(MAKE) -j1 -f $(IDEAS_MAKEFILE) -C $(@D) clean

# Global clean
clean:
	rm -rf docker/docker_build.log
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
	@echo "Example:"
	@echo "  make examples/test TRANSLATION_DIR=test_case ${GREY_COL}# Translate, build, and run tests on C examples ${RESET}"
