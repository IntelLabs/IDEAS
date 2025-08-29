#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))
CARGO_TOML_CMAKE := ${MAKEFILE_DIR}/cargo_toml.cmake

PROVIDER ?= hosted_vllm
MODEL ?= Qwen/Qwen3-Coder-30B-A3B-Instruct
PORT ?= 8000
BASE_URL ?= http://localhost:${PORT}/v1
TRANSLATION_DIR ?= translation.$(shell git --git-dir=${MAKEFILE_DIR}/.git rev-parse HEAD)
TRANSLATE_ARGS ?= algorithm.preproc_strategy=clang generate.max_new_tokens=10000
RUSTFLAGS ?= -Awarnings## Ignore Rust compiler warnings
CFLAGS ?= -w## Ignore C compiler warnings
VERBOSE ?= 0## This is for verbose output in IDEAS.mk

PROJECT_C_FILES := $(wildcard project/src/*.c)
C_FILES := $(notdir ${PROJECT_C_FILES})
RUST_FILES := $(patsubst %.c,%.rs,${C_FILES})
TEST_FILES := $(wildcard test_cases/*.json)
TEST_TIMEOUT ?= 5

# This makefile assumes CURDIR is an example
ifeq (${PROJECT_C_FILES},)
$(error ${CURDIR} does not contain project/src/*.c files! Use -C to specify example directory)
endif


# project
project/translate.log: ;

.PRECIOUS: project/build/%
project/build/%: project/build/CMakeCache.txt ;

.PRECIOUS: project/build/CMakeCache.txt
project/build/CMakeCache.txt: project/CMakeLists.txt ${CARGO_TOML_CMAKE}
	@rm -rf project/build
	cmake -S project -B project/build --log-level=ERROR -DCMAKE_PROJECT_INCLUDE=${CARGO_TOML_CMAKE} -DCMAKE_C_FLAGS="${CFLAGS}" -DCMAKE_EXPORT_COMPILE_COMMANDS=ON

project/build/compile_commands.json: project/build/CMakeCache.txt ;

.PRECIOUS: project/build.log
project/build.log: project/build/CMakeCache.txt
	-cmake --build project/build --target all -- --no-print-directory 2> $@

.PRECIOUS: project/executable
project/executable: project/build/CMakeCache.txt
	-@cp project/build/$(shell grep -E "CMAKE_PROJECT_NAME:STATIC=.*" project/build/CMakeCache.txt | cut -f2 -d"=") $@

# translate
.PHONY: translate
translate: ${TRANSLATION_DIR}/translate.log ;

ifeq (${TRANSLATION_DIR},project)
else
${TRANSLATION_DIR}/translate.log: ${TRANSLATION_DIR}/compile_commands.json \
                                  ${TRANSLATION_DIR}/Cargo.toml \
                                  $(addprefix ${TRANSLATION_DIR}/src/,$(addsuffix .i,${C_FILES}))
	@mkdir -p $(@D)
ifeq (${PROVIDER},hosted_vllm)
	-uv run python -m ideas model.name=${PROVIDER}/${MODEL} model.base_url=${BASE_URL} filename=${TRANSLATION_DIR} $(TRANSLATE_ARGS) 2> $@
else
	-uv run python -m ideas model.name=${PROVIDER}/${MODEL} filename=${TRANSLATION_DIR} $(TRANSLATE_ARGS) 2> $@
endif
endif
.PRECIOUS: ${TRANSLATION_DIR}/compile_commands.json
${TRANSLATION_DIR}/compile_commands.json: project/build/compile_commands.json
	@mkdir -p $(@D)
	@cp $^ $@

.PRECIOUS: ${TRANSLATION_DIR}/Cargo.toml
${TRANSLATION_DIR}/Cargo.toml: project/build/Cargo.toml
	mkdir -p $(@D)
	sed -e "s/\.c\.rs/.rs/g" project/build/Cargo.toml > $@

.PRECIOUS: ${TRANSLATION_DIR}/src/%.c
${TRANSLATION_DIR}/src/%.c: project/src/%.c
	mkdir -p $(@D)
	cp $^ $@

.PRECIOUS: ${TRANSLATION_DIR}/src/%.c.i
${TRANSLATION_DIR}/src/%.c.i: ${TRANSLATION_DIR}/src/%.c project/build/CMakeFiles/TargetDirectories.txt
	cmake --build project/build --target src/$(@F)
	mkdir -p ${TRANSLATION_DIR}/src
	cp $(shell head -n1 project/build/CMakeFiles/TargetDirectories.txt)/src/$(@F) $@


# build
.PHONY: build
build: ${TRANSLATION_DIR}/build.log ;

ifneq (${TRANSLATION_DIR},project)
.PRECIOUS: ${TRANSLATION_DIR}/build.log
${TRANSLATION_DIR}/build.log: ${TRANSLATION_DIR}/translate.log
	-export RUSTFLAGS=${RUSTFLAGS} && cargo build --quiet --manifest-path $(@D)/Cargo.toml 2> $@
endif

# test
RESET := \033[0;0m
GREY_COL := \033[1;30m
RED_COL := \033[1;31m
GREEN_COL := \033[1;32m

.PHONY: test
test: ${TRANSLATION_DIR}/test.log
	@grep -E "^(PASS|FAIL) " ${TRANSLATION_DIR}/test.log | cut -d" " -f1 | sort | uniq -c

.PRECIOUS: ${TRANSLATION_DIR}/test.log
${TRANSLATION_DIR}/test.log: $(addprefix ${TRANSLATION_DIR}/,${TEST_FILES})
	@echo "# ${CURDIR}/${TRANSLATION_DIR}" > $@
	@echo "## ${GREY_COL}test_cases${RESET}" >> $@
	@for test in $(abspath $(addprefix ${TRANSLATION_DIR}/,${TEST_FILES})); \
    do \
      if [ $$(jq -e "(.out | if type == \"boolean\" then [] elif type==\"string\" then [.] else . end) == .ret" $$test) = "true" ] ; then \
        echo "PASS ${GREEN_COL}$$test${RESET}" >> $@ ; \
      else \
        echo "FAIL ${RED_COL}$$test${RESET}" >> $@; \
        if [ ${VERBOSE} -ne 0 ] ; then jq -cM "." $$test >> $@ ; fi ; \
      fi ; \
    done

ifneq (${TRANSLATION_DIR},project)
.PRECIOUS: ${TRANSLATION_DIR}/executable
${TRANSLATION_DIR}/executable: ${TRANSLATION_DIR}/build.log
	-@cp $(shell find $(@D)/target/debug -maxdepth 1 -type f -executable | head -n1) $@
endif

.PRECIOUS: ${TRANSLATION_DIR}/test_cases/%.json
${TRANSLATION_DIR}/test_cases/%.json: ${TRANSLATION_DIR}/executable test_cases/%.json
	@mkdir -p $(@D)
	@jq -r "(.in // []) | join(\"\n\")" test_cases/$(@F) \
    | (timeout ${TEST_TIMEOUT} ${TRANSLATION_DIR}/executable $$(jq -r "(.args // []) | join(\"\\n\")" test_cases/$(@F))) 2>&1 \
    | jq --rawfile output /dev/stdin ".ret = (\$$output | rtrimstr(\"\n\") | split(\"\n\"))" test_cases/$(@F) > $@

.PRECIOUS: test_cases/%.json
test_cases/%.json:
	$(error $@ not found)


# clean
.PHONY: clean
clean:
	rm -rf outputs
	rm -rf project/build project/translate.log project/build.log project/*.json project/executable project/test_cases project/test.log
	rm -rf test.log
ifneq (${TRANSLATION_DIR},project)
	rm -rf ${TRANSLATION_DIR}
endif
