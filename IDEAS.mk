#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))
EXTRACT_INFO_CMAKE := ${MAKEFILE_DIR}/extract_info.cmake

PROVIDER ?= hosted_vllm
MODEL ?= Qwen/Qwen3-Coder-30B-A3B-Instruct
HOST ?= localhost
PORT ?= 8000
BASE_URL ?= http://${HOST}:${PORT}/v1
TRANSLATION_DIR ?= translation.$(shell git --git-dir=${MAKEFILE_DIR}/.git rev-parse HEAD)
ifeq (${PROVIDER},hosted_vllm)
override TRANSLATE_ARGS += model.base_url=${BASE_URL}
override WRAPPER_ARGS += model.base_url=${BASE_URL}
endif
RUSTFLAGS ?= -Awarnings## Ignore Rust compiler warnings
CARGO_NET_OFFLINE ?= true## Cargo offline mode
CFLAGS ?= -w## Ignore C compiler warnings

GIT = git -C ${TRANSLATION_DIR}

TEST_FILES := $(realpath $(wildcard test_vectors/*.json))
TARGETS ?= $(shell find build-ninja -maxdepth 1 -type f -executable -exec basename {} \; | cut -d. -f1 | sed -e "s/^lib//gi")
ifeq (${TARGETS},)
ifeq ($(filter cmake clean,$(MAKECMDGOALS)),)
$(error No TARGETS found! You need to run cmake!)
endif
endif


# cmake
cmake: build-ninja/build.log

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


# init
.PHONY: init
init: $(patsubst %,${TRANSLATION_DIR}/%/init,${TARGETS}) ;
${TRANSLATION_DIR}/%/init: ${TRANSLATION_DIR}/%/src/lib.c | build-ninja/lib%.so.type ;
${TRANSLATION_DIR}/%/init: ${TRANSLATION_DIR}/%/src/main.c | build-ninja/%.type ;

# FIXME: It would be really nice if we could just check out a branch here if the repo already exists and start from there
.PRECIOUS: ${TRANSLATION_DIR}/.git/config
${TRANSLATION_DIR}/.git/config:
	@mkdir -p ${TRANSLATION_DIR}
	${GIT} init --quiet --initial-branch=main
	echo "Cargo.lock\ntarget/\n*.log\n*.jsonl" > ${TRANSLATION_DIR}/.gitignore
	${GIT} add .gitignore
	${GIT} commit --quiet --all --message "Initial commit"

.PRECIOUS: ${TRANSLATION_DIR}/Cargo.toml
${TRANSLATION_DIR}/Cargo.toml: ${TRANSLATION_DIR}/.git/config
	echo -n "[workspace]\nresolver = \"3\"" > $@
	${GIT} add Cargo.toml
	${GIT} commit --quiet --all --message "Created cargo workspace"

.PRECIOUS: ${TRANSLATION_DIR}/%/src/lib.c
${TRANSLATION_DIR}/%/Cargo.toml ${TRANSLATION_DIR}/%/src/lib.c &: | ${TRANSLATION_DIR}/Cargo.toml build-ninja/lib%.so.type
	uv run python -m ideas.init filename=build-ninja/compile_commands.json \
                            crate_type=lib \
                            export_symbols=build-ninja/lib$*.so.symbols \
                            source_priority=build-ninja/lib$*.so.sources \
                            vcs=git \
                            hydra.output_subdir=.init \
                            hydra.run.dir=${TRANSLATION_DIR}/$*

.PRECIOUS: ${TRANSLATION_DIR}/%/src/main.c
${TRANSLATION_DIR}/%/Cargo.toml ${TRANSLATION_DIR}/%/src/main.c &: | ${TRANSLATION_DIR}/Cargo.toml build-ninja/%.type
	uv run python -m ideas.init filename=build-ninja/compile_commands.json \
                            crate_type=bin \
                            export_symbols=build-ninja/$*.symbols \
                            source_priority=build-ninja/$*.sources \
                            vcs=git \
                            hydra.output_subdir=.init \
                            hydra.run.dir=${TRANSLATION_DIR}/$*


# translate
.PHONY: translate
translate: $(patsubst %,${TRANSLATION_DIR}/%/translate,${TARGETS}) ;
${TRANSLATION_DIR}/%/translate: ${TRANSLATION_DIR}/%/src/lib.rs | build-ninja/lib%.so.type ;
${TRANSLATION_DIR}/%/translate: ${TRANSLATION_DIR}/%/src/main.rs | build-ninja/%.type ;

.PRECIOUS: ${TRANSLATION_DIR}/%/src/lib.rs
${TRANSLATION_DIR}/%/src/lib.rs: ${TRANSLATION_DIR}/%/src/lib.c ${TRANSLATION_DIR}/%/tests/test_cases.rs | build-ninja/compile_commands.json build-ninja/lib%.so.symbols build-ninja/lib%.so.sources
	-uv run python -m ideas.translate model.name=${PROVIDER}/${MODEL} \
                                 filename=${TRANSLATION_DIR}/$*/src/lib.c \
                                 vcs=git \
                                 hydra.output_subdir=.translate \
                                 hydra.job.name=translate \
                                 hydra.run.dir=${TRANSLATION_DIR}/$* ${TRANSLATE_ARGS}

.PRECIOUS: ${TRANSLATION_DIR}/%/src/main.rs
${TRANSLATION_DIR}/%/src/main.rs: ${TRANSLATION_DIR}/%/src/main.c ${TRANSLATION_DIR}/%/tests/test_cases.rs | build-ninja/compile_commands.json build-ninja/%.symbols build-ninja/%.sources
	-uv run python -m ideas.translate model.name=${PROVIDER}/${MODEL} \
                                 filename=${TRANSLATION_DIR}/$*/src/main.c \
                                 vcs=git \
                                 hydra.output_subdir=.translate \
                                 hydra.job.name=translate \
                                 hydra.run.dir=${TRANSLATION_DIR}/$* ${TRANSLATE_ARGS}


# wrapper
.PHONY: wrapper
wrapper: $(patsubst %,${TRANSLATION_DIR}/%/wrapper,${TARGETS}) ;
${TRANSLATION_DIR}/%/wrapper: ${TRANSLATION_DIR}/%/src/wrapper.rs ;

.PRECIOUS: ${TRANSLATION_DIR}/%/src/wrapper.rs
${TRANSLATION_DIR}/%/src/wrapper.rs: ${TRANSLATION_DIR}/%/src/lib.rs | build-ninja/lib%.so.symbols
	-uv run python -m ideas.wrapper model.name=${PROVIDER}/${MODEL} \
                               symbols=build-ninja/lib$*.so.symbols \
                               cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                               vcs=git \
                               hydra.output_subdir=.wrapper \
                               hydra.job.name=wrapper \
                               hydra.run.dir=${TRANSLATION_DIR}/$* ${WRAPPER_ARGS}

${TRANSLATION_DIR}/%/src/wrapper.rs: ${TRANSLATION_DIR}/%/src/main.rs | build-ninja/%.symbols
	touch $@

# build
.PHONY: build
build: ${TRANSLATION_DIR}/build.log ;

.PRECIOUS: ${TRANSLATION_DIR}/build.log
${TRANSLATION_DIR}/build.log: $(patsubst %,${TRANSLATION_DIR}/%/build.log,${TARGETS}) ;
	cat $^ > $@

.PRECIOUS: ${TRANSLATION_DIR}/%/build.log
${TRANSLATION_DIR}/%/build.log: ${TRANSLATION_DIR}/%/src/wrapper.rs
	-export RUSTFLAGS=${RUSTFLAGS} && cargo build --quiet --manifest-path ${TRANSLATION_DIR}/$*/Cargo.toml 2> ${TRANSLATION_DIR}/$*/build.log
	@cat ${TRANSLATION_DIR}/$*/build.log

# test
.PHONY: test
test: ${TRANSLATION_DIR}/cargo_test.log ;

.PRECIOUS: ${TRANSLATION_DIR}/cargo_test.log
${TRANSLATION_DIR}/cargo_test.log: ${TRANSLATION_DIR}/build.log $(patsubst %,${TRANSLATION_DIR}/%/cargo_test.log,${TARGETS})
	cat $^ > $@

.PRECIOUS: ${TRANSLATION_DIR}/%/cargo_test.log
${TRANSLATION_DIR}/%/cargo_test.log: ${TRANSLATION_DIR}/%/build.log ${TRANSLATION_DIR}/%/tests/test_cases.rs
	if [ $$(stat -c %s ${TRANSLATION_DIR}/$*/build.log) = 0 ]; then \
    cargo test --manifest-path ${TRANSLATION_DIR}/$*/Cargo.toml --test test_cases | tee $@ ; \
else \
    find test_vectors -name '*.json' -exec echo "test {} ... FAILED" \; | tee $@ ; \
fi \

.PRECIOUS: ${TRANSLATION_DIR}/%/tests/test_cases.rs
${TRANSLATION_DIR}/%/tests/test_cases.rs: | ${TEST_FILES} ${TRANSLATION_DIR}/%/Cargo.toml build-ninja/%.type
	@mkdir -p $(@D)
	cargo add --quiet --manifest-path ${TRANSLATION_DIR}/$*/Cargo.toml --dev assert_cmd@2.0.17 ntest@0.9.3 predicates@3.1.3
	-uv run python -m ideas.convert_tests ${TEST_FILES} --crate_manifest $(realpath ${TRANSLATION_DIR}/$*/Cargo.toml) | rustfmt > $@
	${GIT} add $*/Cargo.toml $*/tests/test_cases.rs
	${GIT} commit --quiet --message "Converted \`$*\` test vectors"

.PRECIOUS: test_vectors/%.json
test_vectors/%.json:
	$(error $@ not found)


# clean
.PHONY: clean
clean:
	rm -rf build-ninja
	find . -name Cargo.toml -exec cargo clean --quiet --manifest-path {} \;
