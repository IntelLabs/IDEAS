#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))
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


RESET := \033[0;0m
GREY_COL := \033[1;30m
RED_COL := \033[1;31m
GREEN_COL := \033[1;32m

PROJECT_C_FILES = $(shell jq -r 'map(.file) | .[] | @text' build-ninja/compile_commands.json)
C_FILES = $(subst ${CURDIR}/test_case/,,${PROJECT_C_FILES})
TEST_FILES := $(wildcard test_vectors/*.json)

AFL_TAG = aflplusplus/aflplusplus:stable
FUZZING_TIMEOUT ?= 60
# Makefile does not like colons in filenames.
FUZZING_TEST_VECTORS := $(subst :,\:, $(wildcard afl/out/default/queue/*))

CRATEIFY_BIN = ${MAKEFILE_DIR}/tools/crateify/target/debug/crateify

.PHONY: FORCE
FORCE:


# cmake
cmake: build-ninja/build.log

build-ninja/translate.log: build-ninja/compile_commands.json
	@$(MAKE) --no-print-directory -f ${IDEAS_MAKEFILE} $(addprefix test_case/,$(addsuffix .i,${C_FILES}))
	@touch $@

.PRECIOUS: build-ninja/%
build-ninja/%: build-ninja/CMakeCache.txt ;

.PRECIOUS: build-ninja/CMakeCache.txt
build-ninja/CMakeCache.txt: test_case/CMakeLists.txt ${CARGO_TOML_CMAKE}
	@rm -rf build-ninja
ifeq ($(wildcard CMakePresets.json),)
	cmake -S test_case -B build-ninja -G Ninja \
          -DCMAKE_PROJECT_TOP_LEVEL_INCLUDES=${CARGO_TOML_CMAKE} \
          -DCMAKE_C_FLAGS="${CFLAGS}" \
          -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
else
	cmake -S . --preset test \
          -DCMAKE_PROJECT_TOP_LEVEL_INCLUDES=${CARGO_TOML_CMAKE} \
          -DCMAKE_C_FLAGS="${CFLAGS}" \
          -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
endif

.PRECIOUS: build-ninja/compile_commands.json
build-ninja/compile_commands.json: build-ninja/CMakeCache.txt ;

.PRECIOUS: build-ninja/build.log
build-ninja/build.log: build-ninja/translate.log
ifeq ($(wildcard CMakePresets.json),)
	-cmake --build build-ninja --target all 2> $@
else
	-cmake --build build-ninja --target all --preset test 2> $@
endif

.PRECIOUS: test_case/%.c.i
test_case/%.c.i: build-ninja/compile_commands.json
	$(shell cat build-ninja/compile_commands.json | \
     jq -r '.[] | select(.file == "${CURDIR}/test_case/$*.c") | .command' | \
     sed -e 's/-o [^ ]*//g' | \
     xargs -I{} echo "{} -E -o $@")

# Add more tests from fuzzing. The procedure is
# 1. Copy test input from the initial JSON test vectors;
# 2. Use afl-cmin to minimize the test corpus so they all have unique execution paths;
# 3. Run afl-fuzz with the minimized seeds with a timeout=FUZZING_TIMEOUT;
# 4. Collect the interesting test vectors from the fuzzing output that provide unique execution paths as JSON files.
.PHONY: add_test_vectors
add_test_vectors: afl/executable afl/seeds afl/fuzzing.log

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
	cmake -DCMAKE_C_COMPILER=afl-cc -DCMAKE_CXX_COMPILER=afl-c++ -S ${CURDIR}/test_case -B ${CURDIR}/afl/build --log-level=ERROR -DCMAKE_C_FLAGS="${CFLAGS}"

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

# Generate seeds for AFL from test vectors by copying stdin.
afl/test_input_orig/%: test_vectors/%.json
	@mkdir -p $(@D)
	@jq -r ".stdin" $< > $@

# Minimize the seed corpus by keeping inputs that activate unique execution paths.
afl/seeds: $(patsubst test_vectors/%.json, afl/test_input_orig/%, ${TEST_FILES})
	@echo "--- Starting with $(words $^) test vectors ---"
	docker run \
	--user $(shell id -u):$(shell id -g) \
	-v ${CURDIR}:${CURDIR} \
	${AFL_TAG} \
	afl-cmin -i $(shell pwd)/$(dir $<) -o $(shell pwd)/$@ -- $(shell pwd)/afl/executable > /dev/null 2>&1

# Fuzzing, then recursively call make to extract test vectors,
#	because we don't know the file names before fuzzing.
afl/fuzzing.log: afl/seeds afl/executable
	@echo "--- Minimized to $(shell find $< -maxdepth 1 -type f | wc -l) test vectors ---"
	-docker run \
	--user $(shell id -u):$(shell id -g) \
	-v ${CURDIR}:${CURDIR} \
	-e AFL_SKIP_CPUFREQ=1 \
	-e AFL_I_DONT_CARE_ABOUT_MISSING_CRASHES=1 \
	-e AFL_SHA1_FILENAMES=1 \
	${AFL_TAG} \
	timeout ${FUZZING_TIMEOUT} afl-fuzz -i $(shell pwd)/$< -o $(shell pwd)/$(@D)/out -- $(shell pwd)/afl/executable > /dev/null 2> $(shell pwd)/$@
	$(MAKE) -j1 -f $(IDEAS_MAKEFILE) extract

# Extract interesting test vectors from fuzzing that increase test converage.
.PHONY: extract
extract: $(patsubst afl/out/default/queue/%,test_vectors/%.json,${FUZZING_TEST_VECTORS})
	@echo "--- Added $(words $^) test vectors in ---"
	@echo "--- $(CURDIR)/test_vectors/ ---"

test_vectors/%.json: afl/out/default/queue/%
	@mkdir -p $(@D)
	@docker run \
	-i \
	--user $(shell id -u):$(shell id -g) \
	-v ${CURDIR}:${CURDIR} \
	${AFL_TAG} \
	$(shell pwd)/afl/executable < $< \
	| jq -n --rawfile input $< --rawfile output /dev/stdin "{argv: [], stdin: \$$input, stdout: {"pattern": \$$output} }" > $@


# init
.PHONY: init
init: ${TRANSLATION_DIR}/Cargo.toml build-ninja/compile_commands.json ${CRATEIFY_BIN}
	@$(MAKE) --no-print-directory -f${IDEAS_MAKEFILE} $(addprefix ${TRANSLATION_DIR}/src/,$(patsubst src/%,%,$(patsubst %.c,%.rs,${C_FILES})))
	${CRATEIFY_BIN} ${TRANSLATION_DIR}/src

.PRECIOUS: ${TRANSLATION_DIR}/Cargo.toml
${TRANSLATION_DIR}/Cargo.toml: build-ninja/Cargo.toml
	@mkdir -p $(@D)
	cp build-ninja/Cargo.toml $@

${TRANSLATION_DIR}/%.rs:
	@mkdir -p $(@D)
	echo 'fn main() {\n    println!("Hello, world!");\n}' > $@

.PRECIOUS: ${CRATEIFY_BIN}
${CRATEIFY_BIN}:
	@cd ${MAKEFILE_DIR}/tools/crateify && cargo build

.PRECIOUS: runner/release/runner
runner/release/runner: runner/Cargo.toml
	@cd runner && cargo build --release --target-dir .

# translate
.PHONY: translate
translate: ${TRANSLATION_DIR}/translate.log ;
${TRANSLATION_DIR}/translate.log: build-ninja/compile_commands.json
	-uv run python -m ideas.translate model.name=${PROVIDER}/${MODEL} filename=build-ninja/compile_commands.json hydra.run.dir=${TRANSLATION_DIR} ${TRANSLATE_ARGS}


# build
.PHONY: build
build: ${TRANSLATION_DIR}/build.log ;

.PRECIOUS: ${TRANSLATION_DIR}/build.log
${TRANSLATION_DIR}/build.log: ${TRANSLATION_DIR}/translate.log ${TRANSLATION_DIR}/Cargo.toml ${CRATEIFY_BIN} FORCE
	${CRATEIFY_BIN} ${TRANSLATION_DIR}/src
	-export RUSTFLAGS=${RUSTFLAGS} && cargo build --quiet --manifest-path $(@D)/Cargo.toml 2> $@


# tests for executables
.PHONY: test
test: ${TRANSLATION_DIR}/cargo_test.log ;

.PRECIOUS: ${TRANSLATION_DIR}/cargo_test.log
${TRANSLATION_DIR}/cargo_test.log: ${TRANSLATION_DIR}/Cargo.toml \
                                   ${TRANSLATION_DIR}/tests/test_cases.rs \
                                   ${TRANSLATION_DIR}/build.log
	@if [ $$(stat -c %s ${TRANSLATION_DIR}/build.log) = 0 ]; then \
      cargo test --manifest-path ${TRANSLATION_DIR}/Cargo.toml --test test_cases | tee $@ ; \
    else \
      find test_vectors -name '*.json' -exec echo "test {} ... FAILED" \; | tee $@ ; \
    fi

.PRECIOUS: ${TRANSLATION_DIR}/tests/test_cases.rs
${TRANSLATION_DIR}/tests/test_cases.rs: ${TEST_FILES}
	@mkdir -p $(@D)
	-uv run python -m ideas.convert_tests $^ | rustfmt > $@

.PRECIOUS: test_vectors/%.json
test_vectors/%.json:
	$(error $@ not found)

# tests for C libraries
.PHONY: test_libc
test_libc: runner/test_libc.log ;

.PRECIOUS: runner/test_libc.log
runner/test_libc.log: build-ninja/build.log \
                      runner/release/runner
	find test_vectors -name '*.json' \
      | sort \
      | xargs -I {} sh -c './runner/release/runner lib -c ../{} -v' \
      | tee $@

# tests for Rust libraries
.PHONY: test_librs
test_librs: runner/test_librs.log ;

.PRECIOUS: runner/test_librs.log
runner/test_librs.log: ${TRANSLATION_DIR}/build.log \
                       runner/release/runner
	find test_vectors -name '*.json' \
      | sort \
      | xargs -I {} sh -c './runner/release/runner -b ${TRANSLATION_DIR}/target/debug lib -c ../{} -v' \
      | tee $@

# repair
.PHONY: repair
repair: ${TRANSLATION_DIR}/translate.log ${TRANSLATION_DIR}/Cargo.toml ${TRANSLATION_DIR}/tests/test_cases.rs
	-uv run python -m ideas.repair model.name=${PROVIDER}/${MODEL} cargo_toml=${TRANSLATION_DIR}/Cargo.toml ${REPAIR_ARGS}

# clean
.PHONY: clean
clean:
	rm -rf $(addprefix test_case/,$(addsuffix .i,${C_FILES}))
	rm -rf build-ninja
	rm -rf ${TRANSLATION_DIR}
