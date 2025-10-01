#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))
PIPELINE_DIR := ${MAKEFILE_DIR}/lib/pipeline_automation
PIPELINE_TAG := ideas/$(shell git rev-list -1 HEAD -- ${PIPELINE_DIR})
CARGO_TOML_CMAKE := ${MAKEFILE_DIR}/cargo_toml.cmake
IDEAS_MAKEFILE := $(MAKEFILE_DIR)/IDEAS.mk

PROVIDER ?= hosted_vllm
MODEL ?= Qwen/Qwen3-Coder-30B-A3B-Instruct
HOST ?= localhost
PORT ?= 8000
BASE_URL ?= http://${HOST}:${PORT}/v1
TRANSLATION_DIR ?= translation.$(shell git --git-dir=${MAKEFILE_DIR}/.git rev-parse HEAD)
ifeq (${PROVIDER},hosted_vllm)
override TRANSLATE_ARGS += model.base_url=${BASE_URL}
override REPAIR_ARGS += model.base_url=${BASE_URL}
endif
RUSTFLAGS ?= -Awarnings## Ignore Rust compiler warnings
CFLAGS ?= -w## Ignore C compiler warnings
VERBOSE ?= 0## This is for verbose output in IDEAS.mk

PROJECT_C_FILES = $(shell jq -r 'map(.file) | .[] | @text' test_case/build/compile_commands.json)
C_FILES = $(subst ${CURDIR}/test_case/,,${PROJECT_C_FILES})
TEST_FILES := $(wildcard test_vectors/*.json)
TEST_TIMEOUT ?= 5

AFL_TAG = aflplusplus/aflplusplus:stable
FUZZING_TIMEOUT ?= 60
# Makefile does not like colons in filenames.
FUZZING_TEST_VECTORS := $(subst :,\:, $(wildcard afl/out/default/queue/*))


.PHONY: FORCE
FORCE:


# project
test_case/translate.log: test_case/build/compile_commands.json
	@$(MAKE) --no-print-directory -f ${IDEAS_MAKEFILE} $(addprefix test_case/,$(addsuffix .i,${C_FILES}))
	@touch $@

.PRECIOUS: test_case/build/%
test_case/build/%: test_case/build/CMakeCache.txt ;

.PRECIOUS: test_case/build/CMakeCache.txt
test_case/build/CMakeCache.txt: test_case/CMakeLists.txt ${CARGO_TOML_CMAKE}
	@rm -rf test_case/build
	cmake -S test_case -B test_case/build --log-level=ERROR -DCMAKE_PROJECT_INCLUDE=${CARGO_TOML_CMAKE} -DCMAKE_C_FLAGS="${CFLAGS}" -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

.PRECIOUS: test_case/build/compile_commands.json
test_case/build/compile_commands.json: test_case/build/CMakeCache.txt ;

.PRECIOUS: test_case/build.log
test_case/build.log: test_case/translate.log
	-cmake --build test_case/build --target all -- --no-print-directory ${CMAKE_BUILD_ARGS} 2> $@

.PRECIOUS: test_case/%.c.i
test_case/%.c.i: test_case/build/compile_commands.json
	$(shell cat test_case/build/compile_commands.json | \
     jq -r '.[] | select(.file == "${CURDIR}/test_case/$*.c") | .command' | \
     sed -e 's/-o [^ ]*//g' | \
     xargs -I{} echo "{} -E -o $@")

# Add more tests from fuzzing. The procedure is
# 1. Copy test input from the initial JSON test cases;
# 2. Use afl-cmin to minimize the test corpus so they all have unique execution paths;
# 3. Run afl-fuzz with the minimized seeds with a timeout=FUZZING_TIMEOUT;
# 4. Collect the interesting test cases from the fuzzing output that provide unique execution paths as JSON files.
.PHONY: add_test_cases
add_test_cases: afl/executable afl/seeds afl/fuzzing.log

.PRECIOUS: afl/build/%
afl/build/%: afl/build/CMakeCache.txt ;

# Use the same source file, but override CC and CXX with AFL's.
# TODO: Use the modified source code in afl/ rather than in test_case/ to fuzz programs with different arguments.
.PRECIOUS: afl/build/CMakeCache.txt
afl/build/CMakeCache.txt: test_case/CMakeLists.txt
	@rm -rf afl/build
	-docker run \
	--user $(shell id -u):$(shell id -g) \
	-v ${CURDIR}:${CURDIR} \
	${AFL_TAG} \
	cmake -DCMAKE_C_COMPILER=afl-cc -DCMAKE_CXX_COMPILER=afl-c++ -S $(shell pwd)/test_case -B $(shell pwd)/afl/build --log-level=ERROR -DCMAKE_C_FLAGS="${CFLAGS}"

.PRECIOUS: afl/build.log
afl/build.log: afl/build/CMakeCache.txt
	-docker run \
	--user $(shell id -u):$(shell id -g) \
	-v ${CURDIR}:${CURDIR} \
	${AFL_TAG} \
	cmake --build $(shell pwd)/afl/build --target all -- --no-print-directory 2> $@

.PRECIOUS: afl/executable
afl/executable: afl/build.log
	-@cp afl/build/$(shell grep -E "CMAKE_PROJECT_NAME:STATIC=.*" afl/build/CMakeCache.txt | cut -f2 -d"=") $@

# Generate seeds for AFL from test cases by copying the input from all test cases.
afl/test_input_orig/%: test_vectors/%.json
	@mkdir -p $(@D)
	@jq -r "(.in // []) | join(\"\n\")" $< > $@

# Minimize the seed corpus by keeping inputs that activate unique execution paths.
afl/seeds: $(patsubst test_vectors/%.json, afl/test_input_orig/%, ${TEST_FILES})
	@echo "--- Starting with $(words $^) test cases ---"
	docker run \
	--user $(shell id -u):$(shell id -g) \
	-v ${CURDIR}:${CURDIR} \
	${AFL_TAG} \
	afl-cmin -i $(shell pwd)/$(dir $<) -o $(shell pwd)/$@ -- $(shell pwd)/afl/executable > /dev/null 2>&1

# Fuzzing, then recursively call make to extract test cases,
#	because we don't know the file names before fuzzing.
afl/fuzzing.log: afl/seeds afl/executable
	@echo "--- Minimized to $(shell find $< -maxdepth 1 -type f | wc -l) test cases ---"
	-docker run \
	--user $(shell id -u):$(shell id -g) \
	-v ${CURDIR}:${CURDIR} \
	-e AFL_SKIP_CPUFREQ=1 \
	-e AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
	-e AFL_SHA1_FILENAMES=1 \
	${AFL_TAG} \
	timeout ${FUZZING_TIMEOUT} afl-fuzz -i $(shell pwd)/$< -o $(shell pwd)/$(@D)/out -- $(shell pwd)/afl/executable > /dev/null 2> $(shell pwd)/$@
	$(MAKE) -j1 -f $(IDEAS_MAKEFILE) extract

# Extract interesting test cases from fuzzing that increase test converage.
.PHONY: extract
extract: $(patsubst afl/out/default/queue/%,test_vectors/%.json,${FUZZING_TEST_VECTORS})
	@echo "--- Added $(words $^) test vectors in ---"
	@echo "--- $(CURDIR)/test_vectors/ ---"

test_vectors/%.json: afl/out/default/queue/%
	@mkdir -p $(@D)
	@./afl/executable < $< \
	| jq -n --rawfile input $< --rawfile output /dev/stdin "{argv: [], stdin: \$$input, stdout: {"pattern": \$$output} }" > $@


# c2rust
# FIXME: We are using a hotfix to run `carge generate_lockfile` on the host.
.PRECIOUS: c2rust/translate.log
c2rust/translate.log: test_case/CMakeLists.txt
	-uv run --with-requirements ${PIPELINE_DIR}/requirements.txt \
            python ${PIPELINE_DIR}/c2rust/invoke_c2rust.py \
                   --container-name ${PIPELINE_TAG}/c2rust \
                   --stream-docker-output $(<D) $(@D) 2> c2rust.log
	mv c2rust.log $@

# init
.PHONY: init
init: ${TRANSLATION_DIR}/Cargo.toml test_case/build/compile_commands.json

	@$(MAKE) --no-print-directory -f${IDEAS_MAKEFILE} $(addprefix ${TRANSLATION_DIR}/,$(patsubst %.c,%.rs,${C_FILES}))

.PRECIOUS: ${TRANSLATION_DIR}/Cargo.toml
${TRANSLATION_DIR}/Cargo.toml: test_case/build/Cargo.toml
	@mkdir -p $(@D)
	cp test_case/build/Cargo.toml $@

${TRANSLATION_DIR}/%.rs:
	@mkdir -p $(@D)
	echo 'fn main() {\n    println!("Hello, world!");\n}' > $@


# translate
.PHONY: translate
translate: ${TRANSLATION_DIR}/translate.log ;

ifeq (${TRANSLATION_DIR},test_case)
else ifeq (${TRANSLATION_DIR},c2rust)
else ifeq (${TRANSLATION_DIR},afl)
else
${TRANSLATION_DIR}/translate.log: test_case/build/compile_commands.json
	-uv run python -m ideas.translate model.name=${PROVIDER}/${MODEL} filename=test_case/build/compile_commands.json hydra.run.dir=${TRANSLATION_DIR} ${TRANSLATE_ARGS}
endif


# build
.PHONY: build
build: ${TRANSLATION_DIR}/build.log ;

ifeq (${TRANSLATION_DIR},test_case)
else ifeq (${TRANSLATION_DIR},afl)
else
.PRECIOUS: ${TRANSLATION_DIR}/build.log
${TRANSLATION_DIR}/build.log: ${TRANSLATION_DIR}/translate.log ${TRANSLATION_DIR}/Cargo.toml FORCE
	-export RUSTFLAGS=${RUSTFLAGS} && cargo build --quiet --manifest-path $(@D)/Cargo.toml 2> $@
endif

# test
RESET := \033[0;0m
GREY_COL := \033[1;30m
RED_COL := \033[1;31m
GREEN_COL := \033[1;32m

.PHONY: test
test: ${TRANSLATION_DIR}/cargo_test.log ;

.PRECIOUS: ${TRANSLATION_DIR}/unsafety.json
${TRANSLATION_DIR}/unsafety.json: ${TRANSLATION_DIR}/build.log
	uv run --with-requirements ${PIPELINE_DIR}/requirements.txt \
           python ${PIPELINE_DIR}/evaluate_unsafe_usage/invoke_unsafety.py \
                  --container-name ${PIPELINE_TAG}/unsafety \
                  $(<D) $@


.PRECIOUS: ${TRANSLATION_DIR}/idiomaticity.json
${TRANSLATION_DIR}/idiomaticity.json: ${TRANSLATION_DIR}/build.log
	uv run --with-requirements ${PIPELINE_DIR}/requirements.txt \
           python ${PIPELINE_DIR}/idiomaticity/invoke_idiomaticity.py \
                  --container-name ${PIPELINE_TAG}/idiomaticity \
                  $(<D) $@

.PRECIOUS: ${TRANSLATION_DIR}/cargo_test.log
${TRANSLATION_DIR}/cargo_test.log: ${TRANSLATION_DIR}/Cargo.toml \
                                   ${TRANSLATION_DIR}/tests/test_cases.rs \
                                   ${TRANSLATION_DIR}/build.log
	@if [ $$(stat -c %s ${TRANSLATION_DIR}/build.log) = 0 ]; then \
      cargo test --manifest-path ${TRANSLATION_DIR}/Cargo.toml --test test_cases > $@ ; \
    else \
      find test_vectors -name '*.json' -exec echo "test {} ... FAILED" \; > $@ ; \
    fi

.PRECIOUS: ${TRANSLATION_DIR}/tests/test_cases.rs
${TRANSLATION_DIR}/tests/test_cases.rs: ${TEST_FILES}
	@mkdir -p $(@D)
	-uv run python -m ideas.convert_tests $^ | rustfmt > $@

.PRECIOUS: test_vectors/%.json
test_vectors/%.json:
	$(error $@ not found)

# repair
.PHONY: repair
repair: ${TRANSLATION_DIR}/translate.log ${TRANSLATION_DIR}/Cargo.toml ${TRANSLATION_DIR}/tests/test_cases.rs
	-uv run python -m ideas.repair model.name=${PROVIDER}/${MODEL} cargo_toml=${TRANSLATION_DIR}/Cargo.toml ${REPAIR_ARGS}

# clean
.PHONY: clean
clean:
	rm -rf test_case/build test_case/translate.log test_case/build.log test_case/*.json
	rm -rf c2rust
ifneq (${TRANSLATION_DIR},test_case)
	rm -rf ${TRANSLATION_DIR}
endif
