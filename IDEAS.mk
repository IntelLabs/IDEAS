#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))
EXTRACT_INFO_CMAKE := ${MAKEFILE_DIR}/extract_info.cmake
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
override WRAPPER_ARGS += model.base_url=${BASE_URL}
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
TARGETS ?= $(shell find build-ninja -maxdepth 1 -type f -executable -exec basename {} \; | cut -d. -f1 | sed -e "s/^lib//gi")
ifeq (${TARGETS},)
ifeq ($(filter cmake clean,$(MAKECMDGOALS)),)
$(error No TARGETS found! You need to run cmake!)
endif
endif

AFL_TAG = aflplusplus/aflplusplus:stable
FUZZING_TIMEOUT ?= 60
# Makefile does not like colons in filenames.
FUZZING_TEST_VECTORS := $(subst :,\:, $(wildcard afl/out/default/queue/*))

CRATEIFY_BIN = ${MAKEFILE_DIR}/tools/crateify/target/debug/crateify


# cmake
cmake: build-ninja/build.log

build-ninja/translate.log: build-ninja/compile_commands.json
	@$(MAKE) --no-print-directory -f ${IDEAS_MAKEFILE} $(addprefix test_case/,$(addsuffix .i,${C_FILES}))
	@touch $@

.PRECIOUS: build-ninja/CMakeCache.txt
build-ninja/CMakeCache.txt: test_case/CMakeLists.txt ${EXTRACT_INFO_CMAKE}
	@rm -rf build-ninja
ifeq ($(wildcard CMakePresets.json),)
	cmake -S test_case -B build-ninja -G Ninja \
      -DCMAKE_BUILD_TYPE=Debug \
      -DCMAKE_C_FLAGS_DEBUG="-g -O0" \
      -DCMAKE_PROJECT_TOP_LEVEL_INCLUDES="${EXTRACT_INFO_CMAKE}" \
      -DCMAKE_C_FLAGS="${CFLAGS}" \
      -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
else
	cmake -S . --preset test \
      -DCMAKE_BUILD_TYPE=Debug \
      -DCMAKE_C_FLAGS_DEBUG="-g -O0" \
      -DCMAKE_PROJECT_TOP_LEVEL_INCLUDES="${EXTRACT_INFO_CMAKE}" \
      -DCMAKE_C_FLAGS="${CFLAGS}" \
      -DCMAKE_EXPORT_COMPILE_COMMANDS=ON
endif

.PRECIOUS: build-ninja/compile_commands.json
build-ninja/compile_commands.json: build-ninja/CMakeCache.txt ;

.PRECIOUS: build-ninja/build.log
build-ninja/build.log: build-ninja/CMakeCache.txt
ifeq ($(wildcard CMakePresets.json),)
	-cmake --build build-ninja --target all 2> $@
else
	-cmake --build build-ninja --target all --preset test 2> $@
endif
	@find build-ninja -maxdepth 1 -type f -executable | \
     xargs -I{} sh -c "nm --extern-only {} | \
                       awk '{if (\$$2 == \"T\") print \$$NF}' | \
                       grep -v ^_ > {}.symbols"

.PRECIOUS: test_case/%.c.i
test_case/%.c.i: build-ninja/compile_commands.json
	cat build-ninja/compile_commands.json | \
     jq -r '.[] | select(.file == "${CURDIR}/test_case/$*.c") | .command' | \
     sed -e 's/-o [^ ]*//g' | \
     xargs -I{} echo "{} -E -o $@" | \
     sh


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
init: $(patsubst %,${TRANSLATION_DIR}/%/init.log,${TARGETS}) ;

.PRECIOUS: ${TRANSLATION_DIR}/Cargo.toml
${TRANSLATION_DIR}/Cargo.toml:
	mkdir -p $(@D)
	echo -n "[workspace]\nresolver = \"3\"" > $@

.PRECIOUS: ${TRANSLATION_DIR}/%/Cargo.toml
${TRANSLATION_DIR}/%/Cargo.toml: | ${TRANSLATION_DIR}/Cargo.toml build-ninja/lib%.so.type
	cargo new --quiet --lib --vcs=none $(@D)
	echo -n "\n[lib]\ncrate-type = [\"lib\", \"cdylib\"]" >> $@
	cargo add --quiet --manifest-path $@ --dev assert_cmd@2.0.17 ntest@0.9.3 predicates@3.1.3
	cargo add --quiet --manifest-path $@ openssl@0.10.75

.PRECIOUS: ${TRANSLATION_DIR}/%/Cargo.toml
${TRANSLATION_DIR}/%/Cargo.toml: | ${TRANSLATION_DIR}/Cargo.toml build-ninja/%.type
	cargo new --quiet --bin --vcs=none $(@D)
	cargo add --quiet --manifest-path $@ --dev assert_cmd@2.0.17 ntest@0.9.3 predicates@3.1.3
	cargo add --quiet --manifest-path $@ openssl@0.10.75

.PRECIOUS: ${TRANSLATION_DIR}/%/src/lib.c
${TRANSLATION_DIR}/%/src/lib.c: ${TRANSLATION_DIR}/%/init.log ;

.PRECIOUS: ${TRANSLATION_DIR}/%/init.log
${TRANSLATION_DIR}/%/init.log: | ${TRANSLATION_DIR}/%/Cargo.toml build-ninja/lib%.so.type
	uv run python -m ideas.init filename=build-ninja/compile_commands.json \
                            export_symbols=build-ninja/lib$*.so.symbols \
                            source_priority=build-ninja/lib$*.so.sources \
                            hydra.output_subdir=.init \
                            hydra.run.dir=${TRANSLATION_DIR}/$*

.PRECIOUS: ${TRANSLATION_DIR}/%/src/main.c
${TRANSLATION_DIR}/%/src/main.c: ${TRANSLATION_DIR}/%/init.log ;

.PRECIOUS: ${TRANSLATION_DIR}/%/init.log
${TRANSLATION_DIR}/%/init.log: | ${TRANSLATION_DIR}/%/Cargo.toml build-ninja/%.type
	uv run python -m ideas.init filename=build-ninja/compile_commands.json \
                            export_symbols=build-ninja/$*.symbols \
                            source_priority=build-ninja/$*.sources \
                            hydra.output_subdir=.init \
                            hydra.run.dir=${TRANSLATION_DIR}/$*

.PRECIOUS: ${CRATEIFY_BIN}
${CRATEIFY_BIN}:
	@cd ${MAKEFILE_DIR}/tools/crateify && cargo build


# translate
.PHONY: translate
translate: $(patsubst %,${TRANSLATION_DIR}/%/translate.log,${TARGETS}) ;

${TRANSLATION_DIR}/translate.log: $(patsubst %,${TRANSLATION_DIR}/%/translate.log,${TARGETS})
	cat $^ > $@

.PRECIOUS: ${TRANSLATION_DIR}/%/src/lib.rs
${TRANSLATION_DIR}/%/src/lib.rs: ${TRANSLATION_DIR}/%/translate.log ;

.PRECIOUS: ${TRANSLATION_DIR}/%/translate.log
${TRANSLATION_DIR}/%/translate.log: | ${TRANSLATION_DIR}/%/src/lib.c build-ninja/compile_commands.json build-ninja/lib%.so.symbols build-ninja/lib%.so.sources
	-uv run python -m ideas.translate_recurrent model.name=${PROVIDER}/${MODEL} \
                                      filename=${TRANSLATION_DIR}/$*/src/lib.c \
                                      hydra.output_subdir=.translate \
                                      hydra.job.name=translate \
                                      hydra.run.dir=${TRANSLATION_DIR}/$* ${TRANSLATE_ARGS}

.PRECIOUS: ${TRANSLATION_DIR}/%/src/main.rs
${TRANSLATION_DIR}/%/src/main.rs: ${TRANSLATION_DIR}/%/translate.log ;

.PRECIOUS: ${TRANSLATION_DIR}/%/translate.log
${TRANSLATION_DIR}/%/translate.log: | ${TRANSLATION_DIR}/%/src/main.c build-ninja/compile_commands.json build-ninja/%.symbols build-ninja/%.sources
	-uv run python -m ideas.translate_recurrent model.name=${PROVIDER}/${MODEL} \
                                      filename=${TRANSLATION_DIR}/$*/src/main.c \
                                      hydra.output_subdir=.translate \
                                      hydra.job.name=translate \
                                      hydra.run.dir=${TRANSLATION_DIR}/$* ${TRANSLATE_ARGS}


# wrapper
.PHONY: wrapper
wrapper: $(patsubst %,${TRANSLATION_DIR}/%/wrapper.log,${TARGETS}) ;

${TRANSLATION_DIR}/%/wrapper.log: ${TRANSLATION_DIR}/%/translate.log | build-ninja/lib%.so.symbols
	@mkdir -p $(@D)/src/wrapper
	-@cat build-ninja/lib$*.so.symbols | xargs -t -I{} bindgen --disable-header-comment --no-doc-comments --no-layout-tests $(@D)/src/lib.c --allowlist-function {} -o $(@D)/src/wrapper/{}.rs
	-@cat build-ninja/lib$*.so.symbols | xargs -t -I{} sed -zEe 's/unsafe extern "C" \{\s+(.*);\s+}/\n\#[unsafe(export_name = "{}")]\n\1 {\n    unimplemented!();\n}/gi' -i $(@D)/src/wrapper/{}.rs
	-@cat build-ninja/lib$*.so.symbols | xargs -t -I{} sed -e 's/pub fn/pub extern "C" fn/gi' -i $(@D)/src/wrapper/{}.rs
	-@cat build-ninja/lib$*.so.symbols | xargs -t -I{} rustfmt ${@D}/src/wrapper/{}.rs
	-uv run python -m ideas.wrapper model.name=${PROVIDER}/${MODEL} \
                               symbols=build-ninja/lib$*.so.symbols \
                               cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                               hydra.output_subdir=.wrapper \
                               hydra.job.name=wrapper \
                               hydra.run.dir=${TRANSLATION_DIR}/$* ${WRAPPER_ARGS}

${TRANSLATION_DIR}/%/wrapper.log: ${TRANSLATION_DIR}/%/translate.log | build-ninja/%.symbols ;


# build
.PHONY: build
build: ${TRANSLATION_DIR}/build.log ;

${TRANSLATION_DIR}/build.log: $(patsubst %,${TRANSLATION_DIR}/%/build.log,${TARGETS})
	cat $^ > $@

${TRANSLATION_DIR}/%/build.log: ${TRANSLATION_DIR}/%/wrapper.log
	-export RUSTFLAGS=${RUSTFLAGS} && cargo build --quiet --manifest-path $(@D)/Cargo.toml 2> $@
	@cat $@

${TRANSLATION_DIR}/target/debug/lib%.so: ${TRANSLATION_DIR}/%/build.log | build-ninja/lib%.so.type ;
${TRANSLATION_DIR}/target/debug/%: ${TRANSLATION_DIR}/%/build.log | build-ninja/%.type ;


# tests for TARGETS
.PHONY: test
test: ${TRANSLATION_DIR}/cargo_test.log ;

.PRECIOUS: ${TRANSLATION_DIR}/cargo_test.log
${TRANSLATION_DIR}/cargo_test.log: ${TRANSLATION_DIR}/build.log $(patsubst %,${TRANSLATION_DIR}/%/tests/test_cases.rs,${TARGETS})
	if [ $$(stat -c %s ${TRANSLATION_DIR}/build.log) = 0 ]; then \
      cargo test --manifest-path ${TRANSLATION_DIR}/Cargo.toml --test test_cases | tee $@ ; \
    else \
      find test_vectors -name '*.json' -exec echo "test {} ... FAILED" \; | tee $@ ; \
    fi

.PRECIOUS: ${TRANSLATION_DIR}/%/tests/test_cases.rs
${TRANSLATION_DIR}/%/tests/test_cases.rs: ${TEST_FILES}
	@mkdir -p $(@D)
	-uv run python -m ideas.convert_tests ${TEST_FILES} | rustfmt > $@

.PRECIOUS: test_vectors/%.json
test_vectors/%.json:
	$(error $@ not found)


# repair
.PHONY: repair
repair: ${TRANSLATION_DIR}/translate.log \
        ${TRANSLATION_DIR}/Cargo.toml \
        ${TRANSLATION_DIR}/tests/test_cases.rs
	-uv run python -m ideas.repair model.name=${PROVIDER}/${MODEL} \
      cargo_toml=${TRANSLATION_DIR}/Cargo.toml \
      ${REPAIR_ARGS}

# clean
.PHONY: clean
clean:
	rm -rf $(addprefix test_case/,$(addsuffix .i,${C_FILES}))
	rm -rf build-ninja
	find . -name Cargo.toml -exec cargo clean --quiet --manifest-path {} \;
