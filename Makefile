#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_DIR := $(realpath $(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
IDEAS_MAKEFILE := ${MAKEFILE_DIR}/IDEAS.mk
include ${MAKEFILE_DIR}/VARIABLES.mk

# Arguments for the inner Make invocation from an example target
IDEAS_MAKE_ARGS = --no-print-directory -f $(IDEAS_MAKEFILE) -C $(@D)

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

include ${MAKEFILE_DIR}/INSTALL.mk
INCLUDE_DOCKER_RULES := 1
unexport INCLUDE_DOCKER_RULES
include ${MAKEFILE_DIR}/docker/DOCKER.mk

.PHONY: vllm/serve
vllm/serve:## Start vLLM server
	${VLLM_LAUNCH} ${VLLM_IMAGE} ${VLLM_RECIPE}

.PHONY: vllm/kill
vllm/kill:## Gracefully stop the running vLLM server
	docker stop ${VLLM_NAME}

.PHONY: FORCE
FORCE:

.PHONY: examples
examples:## Print out examples
examples: $(addsuffix /print,${EXAMPLES}) ;
examples/%/print: FORCE
	@if [ -d "$(@D)" ]; then echo "$(@D)"; fi

.PHONY: examples/bear
examples/bear:## Use bear to intercept compile and linker commands
examples/bear: $(addsuffix /.bear,${EXAMPLES}) ;
ifneq (${VERBOSE},0)
	@echo ""
endif
	@echo "--- Bear ---"
	@find ${EXAMPLES} -maxdepth 2 -path "*/build-ninja/events.jsonl" \( -size +0 -printf 'SUCCEEDED\n' -o -printf 'FAILED\n' \) | sort -r | uniq -c
examples/%/.bear: examples/%/test_case/CMakeLists.txt FORCE ${DOCKER_READY}
	-@$(MAKE) ${IDEAS_MAKE_ARGS} bear
examples/%/bear:## Bear intercept and build specific example
examples/%/bear: FORCE
	-@$(MAKE) --no-print-directory examples/bear EXAMPLES="$(@D)"
examples/libgit2_noconfig_notests/test_case/CMakeLists.txt:  # libgit2 (no config, no tests)
	rsync -rvhz /raid/datasets/P02/libgit2_noconfig_notests/ examples/libgit2_noconfig_notests/

.PHONY: examples/init
examples/init:## Initialize all examples
examples/init: $(addsuffix /.init,${EXAMPLES}) ;
	@echo "# ${TRANSLATION_DIR}"
examples/%/.init: examples/%/.bear FORCE
	-@$(MAKE) ${IDEAS_MAKE_ARGS} init
examples/%/init:## Initialize specific example
examples/%/init: FORCE
	-@$(MAKE) --no-print-directory examples/init EXAMPLES="$(@D)"

.PHONY: examples/testgen
examples/testgen:## Generate I/O tests and check that they pass for all examples
examples/testgen: $(addsuffix /.testgen,${EXAMPLES})
ifneq (${VERBOSE},0)
	@echo ""
endif
	@echo "--- Project Completion Count for ${TESTGEN_CACHE}/cargo_io.jsonl ---"
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TESTGEN_CACHE}/cargo_io.jsonl' -exec ./scripts/test_log_stats.sh {} + | cut -d" " -f1 | sort | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TESTGEN_CACHE}/cargo_io.jsonl' -exec ./scripts/test_log_stats.sh {} + | egrep -v "^complete" | sort | sed 's|/${TESTGEN_CACHE}/cargo_io.jsonl$$||'
endif
	@echo ""
	@echo "--- Aggregated Test Count for ${TESTGEN_CACHE}/cargo_io.jsonl ---"
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TESTGEN_CACHE}/cargo_io.jsonl' -exec cat {} + \
      | jq -r 'select(.type=="test" and (.event=="ok" or .event=="failed")) | if .event == "ok" then "ok" elif .reason == "time limit exceeded" then "TIMEOUT" else "FAILED" end' \
      | LC_ALL=C sort -r | uniq -c
examples/%/.testgen: examples/%/.bear FORCE
	-@$(MAKE) ${IDEAS_MAKE_ARGS} testgen TRANSLATION_DIR=${TESTGEN_CACHE}
examples/%/testgen:## Generate I/O tests and check that they pass for a specific example
examples/%/testgen: FORCE
	-@$(MAKE) --no-print-directory examples/testgen EXAMPLES="$(@D)"


.PHONY: examples/translate
examples/translate:## Translate all examples
examples/translate: $(addsuffix /.translate,${EXAMPLES})
ifneq (${VERBOSE},0)
	@echo ""
endif
	@echo "--- Translation Count for ${TRANSLATION_DIR} ---"
	@find ${EXAMPLES} -maxdepth 3 -path "*/${TRANSLATION_DIR}/*/translate.log" -exec grep -H '\[ideas.translate\]' {} + | \
      sed -En -e 's#^(.+)/[^/]+/[^/]+/translate\.log:.* - Translated .* `([^`]+)`.*:.*#translated \1 \2#p' \
              -e 's#^(.+)/[^/]+/[^/]+/translate\.log:.* - Failed .* `([^`]+)`.*:.*#FAILED \1 \2#p' | \
      cut -d" " -f1 | sort | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -maxdepth 3 -path "*/${TRANSLATION_DIR}/*/translate.log" -exec grep -H '\[ideas.translate\]' {} + | \
      sed -En -e 's#^(.+)/[^/]+/[^/]+/translate\.log:.* - Translated .* `([^`]+)`.*:.*#translated \1 \2#p' \
              -e 's#^(.+)/[^/]+/[^/]+/translate\.log:.* - Failed .* `([^`]+)`.*:.*#FAILED \1 \2#p' | \
      grep -v "^translated" | sort
endif
examples/%/.translate: examples/%/.bear FORCE
	-@$(MAKE) ${IDEAS_MAKE_ARGS} translate
examples/%/translate:## Translate specific example
examples/%/translate: FORCE
	-@$(MAKE) --no-print-directory examples/translate EXAMPLES="$(@D)"


.PHONY: examples/build
examples/build:## Build all translated examples
examples/build: $(addsuffix /.build,${EXAMPLES})
ifneq (${VERBOSE},0)
	@echo ""
endif
	@echo "--- Project Builds for ${TRANSLATION_DIR}/build.log ---"
	@find ${EXAMPLES} -maxdepth 2 -path "*/${TRANSLATION_DIR}/build.log" \( -size 0 -printf 'builds\n' -o -printf 'BROKEN\n' \) | sort -r | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -maxdepth 2 -path "*/${TRANSLATION_DIR}/build.log" -size +0 -printf 'BROKEN %h\n' | sed 's|/${TRANSLATION_DIR}$$||' | sort
endif
examples/%/.build: examples/%/.bear FORCE
	-@$(MAKE) ${IDEAS_MAKE_ARGS} build
examples/%/build:## Build specific translated example
examples/%/build: FORCE
	-@$(MAKE) --no-print-directory examples/build EXAMPLES="$(@D)"

.PHONY: examples/baseline
examples/baseline:## Run the test vectors of all examples against their C build
examples/baseline: $(addsuffix /.baseline,${EXAMPLES})
ifneq (${VERBOSE},0)
	@echo ""
endif
	@echo "--- Project Completion Count for build-ninja/baseline.json ---"
	@find ${EXAMPLES} -maxdepth 2 -path '*/build-ninja/baseline.json' \( -size +0 -printf 'complete\n' -o -printf 'EMPTY\n' \) | LC_ALL=C sort -r | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -maxdepth 2 -path '*/build-ninja/baseline.json' -size 0 -printf 'EMPTY %h\n' | sort
endif
	@echo ""
	@echo "--- Aggregated Test Count for build-ninja/baseline.json ---"
	@find ${EXAMPLES} -maxdepth 2 -path '*/build-ninja/baseline.json' -exec cat {} + \
      | jq -r '.[].result' | sort | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -maxdepth 2 -path '*/build-ninja/baseline.json' -size +0 -print0 \
      | xargs -0 -r jq -r 'to_entries[] | select(.value.result != "Pass" and .value.result != "Skip") | "\(.value.result) \(input_filename) \(.key)"' \
      | sed 's|/build-ninja/baseline.json | |' | sort
endif
examples/%/.baseline: examples/%/.bear FORCE
	-@$(MAKE) ${IDEAS_MAKE_ARGS} baseline
examples/%/baseline:## Run the test vectors of a specific example against its C build
examples/%/baseline: FORCE
	-@$(MAKE) --no-print-directory examples/baseline EXAMPLES="$(@D)"

.PHONY: examples/test
examples/test:## Test all translated examples
examples/test: $(addsuffix /.test,${EXAMPLES})
ifneq (${VERBOSE},0)
	@echo ""
endif
	@echo "--- Project Completion Count for ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl ---"
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl' -exec ./scripts/test_log_stats.sh {} + | cut -d" " -f1 | sort | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl' -exec ./scripts/test_log_stats.sh {} + | egrep -v "^complete" | sort | sed 's|/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl$$||' | sort
endif
	@echo ""
	@echo "--- Aggregated Test Count for ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl ---"
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl' -exec cat {} + \
      | jq -r 'select(.type=="test" and (.event=="ok" or .event=="failed")) | if .event == "ok" then "ok" elif .reason == "time limit exceeded" then "TIMEOUT" else "FAILED" end' \
      | LC_ALL=C sort -r | uniq -c
examples/%/.test: examples/%/.bear FORCE
	-@$(MAKE) ${IDEAS_MAKE_ARGS} test
examples/%/test:## Test specific translated example
examples/%/test: FORCE
	-@$(MAKE) --no-print-directory examples/test EXAMPLES="$(@D)"

.PHONY: examples/cost
examples/cost:## Print cost of all translated examples
examples/cost: $(addsuffix /.cost,${EXAMPLES})
	@echo "--- Aggregated Cost for ${TRANSLATION_DIR} ---"
	@find ${EXAMPLES} -maxdepth 2 -path "*/${TRANSLATION_DIR}/cost.tsv" -exec cat {} + | sort -k1 | tr -d '$$,' | datamash -g1 sum 3 sum 4 sum 5 sum 6 | awk '{printf "%28s $$%10.4f %12\047d tok ( %12\047d in / %12\047d out)\n",$$1,$$2,$$3,$$4,$$5}'
examples/%/.cost: examples/%/.bear FORCE
	-@$(MAKE) ${IDEAS_MAKE_ARGS} cost
examples/%/cost:## Print cost for specific translated example
examples/%/cost: FORCE
	-@$(MAKE) --no-print-directory examples/cost EXAMPLES="$(@D)"

.PHONY: examples/stats
examples/stats:## Print translation stats for all examples
examples/stats:
	-@$(MAKE) --no-print-directory examples/translate
	@echo ""
	-@$(MAKE) --no-print-directory examples/build
	@echo ""
	-@$(MAKE) --no-print-directory examples/test
	@echo ""
	-@$(MAKE) --no-print-directory examples/cost VERBOSE=0

examples/%/stats:##Print translation stats for specific example
examples/%/stats: FORCE
	-@$(MAKE) --no-print-directory examples/stats EXAMPLES="$(@D)"

.PHONY: examples/reset
examples/reset:## Start a fresh run in ${TRANSLATION_DIR} of all examples, keeping the old one on a branch
examples/reset: $(addsuffix /reset,${EXAMPLES})
examples/%/reset:## Start a fresh run in ${TRANSLATION_DIR} of specific example, keeping the old one on a branch
examples/%/reset: FORCE
	-@$(MAKE) ${IDEAS_MAKE_ARGS} reset

.PHONY: examples/clean
examples/clean:## Clean all examples
examples/clean: $(addsuffix /clean,${EXAMPLES})
examples/%/clean:## Clean specific example
examples/%/clean: FORCE
	-@$(MAKE) ${IDEAS_MAKE_ARGS} clean

# Global clean
clean:## Clean Docker and Rust artifacts
clean:
	find examples -type d -exec test -e '{}/Cargo.toml' \; -prune -exec cargo clean --manifest-path '{}/Cargo.toml' \;

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
