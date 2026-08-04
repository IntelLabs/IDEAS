#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_DIR := $(realpath $(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
include ${MAKEFILE_DIR}/VARIABLES.mk

BEAR_VERSION = 4.1.5## bear version to install

HF_TOKEN = ## Hugging Face token (optional, recommended for faster download speed)
HF_CACHE = ${HOME}/.cache/huggingface## Hugging Face cache dir on host
VLLM_NAME = local-model## Name of the vLLM container
VLLM_LAUNCH = docker run -it --rm --name ${VLLM_NAME} \
    --runtime nvidia --gpus all --ipc=host \
    -v ${HF_CACHE}:/root/.cache/huggingface \
    -e HF_TOKEN \
    -p ${PORT}:${PORT}## vLLM launch preamble
VLLM_IMAGE = vllm/vllm-openai@sha256:251eba5cc7c12fed0b75da22a9240e582b1c9e39f6fbc064f86781b963bd814f## vLLM image
VLLM_RECIPE = zai-org/GLM-5.2-FP8 \
    --revision ba978f7d347eaf65d22f1a86833408afdb953541 \
    --tensor-parallel-size 8 \
    --kv-cache-dtype fp8 \
    --reasoning-parser glm45 \
    --max-model-len auto## See https://recipes.vllm.ai/

EXAMPLES_DIR := examples/Test-Corpus/Public-Tests
EXAMPLES_BATTERY := B02_
ALL_EXAMPLES := $(sort $(patsubst %/test_case,%,$(shell find ${EXAMPLES_DIR} -maxdepth 3 -name test_case -type d | grep -E '(${EXAMPLES_BATTERY})')))
EXAMPLES ?= ${ALL_EXAMPLES}## List of examples to run on

ifeq ($(EXAMPLES),)
$(warning No projects found in ${EXAMPLES_DIR}. You may need to re-run commands!)
endif


all: help ;

.PHONY: docker/build
docker/build:## Build translation Docker image
docker/build: docker/docker_build.log

.PRECIOUS: docker/docker_build.log
docker/docker_build.log: docker/ideas.Dockerfile uv.lock pyproject.toml
	cp uv.lock pyproject.toml docker/
	cd docker && docker build --build-arg USER_UID=$(shell id -u) \
                         --build-arg USER_GID=$(shell id -g) \
                         -f ideas.Dockerfile -t ideas-$(shell id -u) .
	rm docker/uv.lock docker/pyproject.toml
	docker images --quiet ideas-$(shell id -u):latest > $@

.PHONY: examples/docker
examples/docker:## Mount all examples to the translation Docker image
examples/docker: docker/docker_build.log
	mkdir -p $(foreach ex,${EXAMPLES},${MAKEFILE_DIR}/${ex}/${TRANSLATION_DIR})
	${DOCKER_RUN} \
      --mount type=tmpfs,dst=${MAKEFILE_DIR}/examples \
      $(foreach ex,${EXAMPLES},\
        --mount type=tmpfs,dst=${MAKEFILE_DIR}/${ex} \
        --mount type=bind,src=${MAKEFILE_DIR}/${ex}/test_case,dst=${MAKEFILE_DIR}/${ex}/test_case \
        --mount type=bind,src=${MAKEFILE_DIR}/${ex}/${TRANSLATION_DIR},dst=${MAKEFILE_DIR}/${ex}/${TRANSLATION_DIR}) \
      --env TRANSLATION_DIR \
      --env "EXAMPLES=${EXAMPLES}" \
      -it ${DOCKER_IMAGE} bash

examples/%/docker:##Mount specific example to translation Docker image
examples/%/docker: docker/docker_build.log
	mkdir -p ${MAKEFILE_DIR}/$(@D)/${TRANSLATION_DIR}
	${DOCKER_RUN} \
      --mount type=tmpfs,dst=${MAKEFILE_DIR}/examples \
      --mount type=tmpfs,dst=${MAKEFILE_DIR}/$(@D) \
      --mount type=bind,src=${MAKEFILE_DIR}/$(@D)/test_case,dst=${MAKEFILE_DIR}/$(@D)/test_case \
      --mount type=bind,src=${MAKEFILE_DIR}/$(@D)/${TRANSLATION_DIR},dst=${MAKEFILE_DIR}/$(@D)/${TRANSLATION_DIR} \
      --env TRANSLATION_DIR \
      --env EXAMPLES=$(@D) \
      -it ${DOCKER_IMAGE} bash

.PHONY: vllm/serve
vllm/serve:## Start vLLM server
	${VLLM_LAUNCH} ${VLLM_IMAGE} ${VLLM_RECIPE}

.PHONY: vllm/kill
vllm/kill:## Gracefully stop the running vLLM server
	docker stop ${VLLM_NAME}

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
	apt install libpcre3-dev libpcre2-dev

.PHONY: install-bear
install-bear:## Install bear ${BEAR_VERSION} from source (requires Rust)
	git clone --branch ${BEAR_VERSION} --depth 1 https://github.com/rizsotto/Bear /tmp/bear
	cd /tmp/bear && cargo build --release && ./scripts/install.sh
	rm -rf /tmp/bear


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
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) bear
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) init


.PHONY: examples/bear
examples/bear:## Use bear to intercept compile and linker commands
examples/bear: $(addsuffix /bear,${EXAMPLES}) ;
ifneq (${VERBOSE},0)
	@echo ""
endif
	@echo "--- Bear ---"
	@find ${EXAMPLES} -maxdepth 2 -path "*/build-ninja/events.jsonl" \( -size +0 -printf 'SUCCEEDED\n' -o -printf 'FAILED\n' \) | sort -r | uniq -c
examples/%/bear:## Bear intercept and build specific example
examples/%/bear: FORCE
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) bear


.PHONY: examples/testgen
examples/testgen:## Generate I/O test vectors for all targets in all C examples with an agent
examples/testgen: $(addsuffix /testgen,${EXAMPLES})
examples/%/testgen:## Generate I/O test vectors for all targets in a specific C example with an agent
examples/%/testgen: FORCE
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) bear
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) testgen


.PHONY: examples/translate
examples/translate:## Translate all examples
examples/translate: $(addsuffix /translate,${EXAMPLES})
ifneq (${VERBOSE},0)
	@echo ""
endif
	@echo "--- Translation Count for ${TRANSLATION_DIR} ---"
	@find ${EXAMPLES} -maxdepth 2 -path "*/${TRANSLATION_DIR}/translate.log" | wc -l
examples/%/translate:## Translate specific example
examples/%/translate: FORCE
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) bear
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) translate


.PHONY: examples/build
examples/build:## Build all translated examples
examples/build: $(addsuffix /build,${EXAMPLES})
ifneq (${VERBOSE},0)
	@echo ""
endif
	@echo "--- Project Builds for ${TRANSLATION_DIR}/build.log ---"
	@find ${EXAMPLES} -maxdepth 2 -path "*/${TRANSLATION_DIR}/build.log" \( -size 0 -printf 'builds\n' -o -printf 'BROKEN\n' \) | sort -r | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -maxdepth 2 -path "*/${TRANSLATION_DIR}/build.log" -size +0 -printf 'BROKEN %h\n' | sed 's|/${TRANSLATION_DIR}$$||' | sort
endif
examples/%/build:## Build specific translated example
examples/%/build: FORCE
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) bear
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) build


.PHONY: examples/test
examples/test:## Test all translated examples
examples/test: $(addsuffix /test,${EXAMPLES})
ifneq (${VERBOSE},0)
	@echo ""
endif
	@echo "--- Project Completion Count for ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl ---"
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl' -exec ./scripts/test_log_stats.sh {} + | cut -d" " -f1 | sort | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl' -exec ./scripts/test_log_stats.sh {} + | egrep -v "^complete" | sort | sed 's|/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl$$||'
endif
	@echo ""
	@echo "--- Aggregated Test Count for ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl ---"
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl' -exec cat {} + \
	  | jq -r 'select(.type=="test" and (.event=="ok" or .event=="failed")) | .event' \
	  | sed 's/failed/FAILED/' | sort -r | uniq -c
examples/%/test:## Test specific translated example
examples/%/test: FORCE
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) bear
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) test

.PHONY: examples/cost
examples/cost:## Print cost of all translated examples
examples/cost: $(addsuffix /cost,${EXAMPLES})
	@echo "--- Aggregated Cost for ${TRANSLATION_DIR} ---"
	@find ${EXAMPLES} -maxdepth 2 -path "*/${TRANSLATION_DIR}/cost.tsv" -exec cat {} + | sort -k1 | tr -d '$$,' | datamash -g1 sum 3 sum 4 sum 5 sum 6 | awk '{printf "%28s $$%10.4f %12\047d tok ( %12\047d in / %12\047d out)\n",$$1,$$2,$$3,$$4,$$5}'
examples/%/cost:## Print cost for specific translated example
examples/%/cost: FORCE
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) bear
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) cost

.PHONY: examples/stats
examples/stats:## Print translation stats for all examples
examples/stats:
	-@$(MAKE) --no-print-directory examples/build
	@echo ""
	-@$(MAKE) --no-print-directory examples/test
	@echo ""
	-@$(MAKE) --no-print-directory examples/cost VERBOSE=0

examples/%/stats:##Print translation stats for specific example
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) build VERBOSE=1
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) test VERBOSE=1
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) cost VERBOSE=1



.PHONY: examples/clean
examples/clean:## Clean all examples
examples/clean: $(addsuffix /clean,${EXAMPLES})
examples/%/clean:## Clean specific example
examples/%/clean: FORCE
	-@$(MAKE) --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D) clean

# Global clean
clean:
	rm -rf docker/docker_build.log
	rm -rf examples
	git checkout HEAD examples

# help
RESET := \033[0;0m
CYAN_COL := \033[0;36m
YELLOW_COL:= \033[0;33m
GREY_COL := \033[1;32m
help:
	@echo "Usage:"
	@echo "  make ${CYAN_COL}[target] ${YELLOW_COL}[variables]${RESET}"
	@echo ""
	@echo "Targets:"
	@grep -hE "^[a-zA-Z/_%%]+:.*?##.*$$" ${MAKEFILE_LIST} \
     | awk 'BEGIN { FS=":.*##" } ; \
          { printf "  ${CYAN_COL}%-30s${RESET}%s\n", $$1, $$2 }'
	@echo ""
	@echo "Variables:"
	@grep -hE "^[a-zA-Z_]+ [:?!+]?=.*?##.*$$" ${MAKEFILE_LIST} \
     | awk 'BEGIN { FS=" [:?!+]?= |##" } ; \
          { printf "  ${YELLOW_COL}%-30s${RESET}%s ${GREY_COL}(default: %s)${RESET}\n", $$1, $$3, $$2}' \
     | sort
	@echo ""
	@echo "Example:"
	@echo "  make examples/test TRANSLATION_DIR=my_translation ${GREY_COL}# Translate, build, and run tests on C examples ${RESET}"
