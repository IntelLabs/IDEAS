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
    -p ${VLLM_PORT}:${VLLM_PORT}## vLLM launch preamble
VLLM_IMAGE = vllm/vllm-openai@sha256:4a2f33a884222f7049b983263ad9976f89452bb81affecf5b67d89ad35c1bc31## vLLM image
VLLM_RECIPE = Qwen/Qwen3.8-27B \
    --revision 1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0 \
    --tensor-parallel-size 8 \
    --max-model-len auto \
    --enable-auto-tool-choice \
    --tool-call-parser qwen3_xml \
    --reasoning-parser qwen3 \
    --mm-encoder-tp-mode data## See https://recipes.vllm.ai/

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

MODEL_PRICES_URL := https://raw.githubusercontent.com/BerriAI/litellm/f2118fc7670ba3fa736d9b8f4deef9077a45a720/model_prices_and_context_window.json
MODEL_PRICES_SHA256 := 91da1a98e194e0d7c436aaf970800c8e734d70cc78027cfe314d5b27dfaf9a4c

src/ideas/model_prices.json:
	curl -fsSL --retry 3 -o $@.tmp ${MODEL_PRICES_URL}
	echo "${MODEL_PRICES_SHA256}  $@.tmp" | sha256sum --check --quiet
	mv $@.tmp $@

PYTEST_ARGS = ## Args to pass to pytest

.PHONY: test
test:## Run the IDEAS test suite with the translation toolchain
test: src/ideas/model_prices.json
	uv run pytest ${PYTEST_ARGS}

.PHONY: vllm/serve
vllm/serve:## Start vLLM server
	${VLLM_LAUNCH} ${VLLM_IMAGE} ${VLLM_RECIPE} --port ${VLLM_PORT}

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
examples/%/.bear: examples/%/test_case/CMakeLists.txt FORCE ${DOCKER_READY} src/ideas/model_prices.json
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
	@echo "--- Project Completion Count for ${TESTGEN_CACHE}/testgen_io.jsonl ---"
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TESTGEN_CACHE}/testgen_io.jsonl' -exec ./scripts/test_log_stats.sh {} + | cut -d" " -f1 | sort | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TESTGEN_CACHE}/testgen_io.jsonl' -exec ./scripts/test_log_stats.sh {} + | egrep -v "^complete" | sort | sed 's|/${TESTGEN_CACHE}/testgen_io.jsonl$$||'
endif
	@echo ""
	@echo "--- Aggregated Test Count for ${TESTGEN_CACHE}/testgen_io.jsonl ---"
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TESTGEN_CACHE}/testgen_io.jsonl' -exec cat {} + \
      | jq -r 'select(.type=="test" and (.event=="ok" or .event=="failed")) | if .event == "ok" then "ok" elif .reason == "time limit exceeded" then "TIMEOUT" else "FAILED" end' \
      | LC_ALL=C sort -r | uniq -c
	@echo ""
	@echo "--- Branch Coverage for ${TESTGEN_CACHE}/testgen_io.tsv ---"
	@find ${EXAMPLES} -maxdepth 2 -path '*/${TESTGEN_CACHE}/testgen_io.tsv' -print | sort | ./scripts/coverage_stats.sh
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
	@echo ""
	@echo "--- Translation Attempts Exhausted for ${TRANSLATION_DIR} ---"
	@find ${EXAMPLES} -maxdepth 3 -path "*/${TRANSLATION_DIR}/*/translate.log" -exec grep -H -E '\(([0-9]+)/\1\)' {} + | \
      awk -v dir="${TRANSLATION_DIR}" '\
        {\
          colon = index($$0, ":");\
          path = substr($$0, 1, colon - 1);\
          message = substr($$0, colon + 1);\
          needle = "/" dir "/";\
          position = index(path, needle);\
          if (!position) next;\
          path = substr(path, 1, position - 1) "/" substr(path, position + length(needle));\
          count = split(path, parts, "/");\
          project = parts[1];\
          for (i = 2; i <= count - 2; i++) project = project "/" parts[i];\
          target = parts[count - 1];\
          while (match(message, /`[^`]+`/)) {\
            symbol = substr(message, RSTART + 1, RLENGTH - 2);\
            message = substr(message, RSTART + RLENGTH);\
            if (symbol ~ /^c:/) print project "\t" target "\t" symbol;\
          }\
        }' | sort -s -k1,1 -k2,2 | \
      awk -F '\t' '\
        BEGIN { OFS = " " }\
        {\
          key = $$1 OFS $$2;\
          if (key != previous && previous != "") print output;\
          if (key != previous) { output = "EXHAUSTED " $$1 " " $$2; previous = key }\
          if (!seen[key SUBSEP $$3]++) output = output " `" $$3 "`";\
        }\
        END { if (previous != "") print output }'
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

.PHONY: examples/idiomaticity
examples/idiomaticity:## Score the idiomaticity of all translated examples
examples/idiomaticity: $(addsuffix /.idiomaticity,${EXAMPLES})
ifneq (${VERBOSE},0)
	@echo ""
endif
	@echo "--- Crate Completion Count for ${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json ---"
	@find ${EXAMPLES} -mindepth 3 -maxdepth 3 -path '*/${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json' \( -size +0 -printf 'scored\n' -o -printf 'EMPTY\n' \) | LC_ALL=C sort -r | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -mindepth 3 -maxdepth 3 -path '*/${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json' -size 0 -printf 'EMPTY %h\n' | sort
endif
	@echo ""
	@echo "--- Mean Level per Dimension for ${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json ---"
	@find ${EXAMPLES} -mindepth 3 -maxdepth 3 -path '*/${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json' -exec cat {} + \
      | jq -r '.dimensions | to_entries[] | "\(.key)\t\(.value.level)"' \
      | LC_ALL=C sort | datamash -g1 count 2 mean 2 \
      | awk '{printf "  %-40s %6d crates %6.2f\n",$$1,$$2,$$3}'
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -mindepth 3 -maxdepth 3 -path '*/${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json' -size +0 -print0 \
      | xargs -0 -r jq -r '"\(input_filename) \(.crate) \([.dimensions | keys_unsorted[] as $$k | "\($$k[0:2] | ascii_upcase)=\(.[$$k].level)"] | join(" ")) mean=\(.mean_level * 100 | round / 100)"' \
      | sed 's|/${TRANSLATION_DIR}/[^/ ]*/idiomaticity.json||' | sort
endif
	@echo ""
	@echo "--- D3 Gate for ${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json ---"
	@find ${EXAMPLES} -mindepth 3 -maxdepth 3 -path '*/${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json' -exec cat {} + \
      | jq -r 'if ([.gate_findings[].tool] | any(. == "rapx")) then "UB-GATED" elif (.gate_findings | length) > 0 then "GATED" else "ungated" end' | LC_ALL=C sort | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -mindepth 3 -maxdepth 3 -path '*/${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json' -size +0 -print0 \
      | xargs -0 -r jq -r 'select((.gate_findings | length) > 0) | "GATED \(input_filename) \(.crate) \(.gate_findings | length) finding(s): \([.gate_findings[].lint] | unique | join(", "))"' \
      | sed 's|/${TRANSLATION_DIR}/[^/ ]*/idiomaticity.json||' | sort
endif
	@echo ""
	@echo "--- UB Scan for ${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json ---"
	@find ${EXAMPLES} -mindepth 3 -maxdepth 3 -path '*/${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json' -exec cat {} + \
      | jq -r '.ub_scan | if . == "clean" or . == "skipped" then . else ascii_upcase end' | LC_ALL=C sort | uniq -c
ifneq (${VERBOSE},0)
	@echo ""
	@find ${EXAMPLES} -mindepth 3 -maxdepth 3 -path '*/${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json' -size +0 -print0 \
      | xargs -0 -r jq -r 'select(.ub_scan != "clean" and .ub_scan != "skipped") | "\(.ub_scan | ascii_upcase) \(input_filename) \(.crate)"' \
      | sed 's|/${TRANSLATION_DIR}/[^/ ]*/idiomaticity.json||' | sort
endif
	@echo ""
	@echo "--- Judge Cost for ${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json ---"
	@find ${EXAMPLES} -mindepth 3 -maxdepth 3 -path '*/${TRANSLATION_DIR}/*${IDIOMATICITY_SUFFIX}/idiomaticity.json' -exec cat {} + \
      | jq -r '.usage // {} | [.cost // 0, .total_tokens // 0, .prompt_tokens // 0, .completion_tokens // 0, .cost_missing // 0] | @tsv' \
      | datamash sum 1 sum 2 sum 3 sum 4 sum 5 \
      | awk '{printf "  $$%10.4f %12\047d tok ( %12\047d in / %12\047d out)%s\n",$$1,$$2,$$3,$$4,($$5 ? sprintf(" (%d unpriced)",$$5) : "")}'
examples/%/.idiomaticity: examples/%/.bear FORCE
	-@$(MAKE) ${IDEAS_MAKE_ARGS} idiomaticity
examples/%/idiomaticity:## Score the idiomaticity of specific translated example
examples/%/idiomaticity: FORCE
	-@$(MAKE) --no-print-directory examples/idiomaticity EXAMPLES="$(@D)"

.PHONY: examples/stats
examples/stats:## Print translation stats for all examples
examples/stats:
	@$(MAKE) --no-print-directory examples/translate
	@echo ""
	@$(MAKE) --no-print-directory examples/build
	@echo ""
	@$(MAKE) --no-print-directory examples/test
	@echo ""
	@$(MAKE) --no-print-directory examples/cost VERBOSE=0
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
