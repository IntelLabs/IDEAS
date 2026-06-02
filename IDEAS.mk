#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))
EXTRACT_INFO_CMAKE := ${MAKEFILE_DIR}/extract_info.cmake
AGENTS_MAKEFILE := $(MAKEFILE_DIR)/AGENTS.mk

PROVIDER ?= hosted_vllm
MODEL ?= Qwen/Qwen3.5-397B-A17B
HOST ?= localhost
PORT ?= 8000
BASE_URL ?= http://${HOST}:${PORT}/v1
TRANSLATION_DIR ?= translation.$(shell git --git-dir=${MAKEFILE_DIR}/.git rev-parse HEAD)
ifeq (${PROVIDER},hosted_vllm)
override TRANSLATE_ARGS += model.base_url=${BASE_URL}
endif
RUSTFLAGS ?= -Awarnings## Ignore Rust compiler warnings
CARGO_NET_OFFLINE ?= true## Cargo offline mode
CFLAGS ?= -w## Ignore C compiler warnings
LARGE_PROJECT ?= 0## Disable translation-time tests and enable context compression
export EXTRACT_INFO_CMAKE CFLAGS LARGE_PROJECT

VCS ?= git
GIT_AUTHOR_NAME ?= ideas
GIT_AUTHOR_EMAIL ?= ideas@localhost
export GIT_AUTHOR_NAME GIT_AUTHOR_EMAIL

ifeq ($(LARGE_PROJECT),1)
TRANSLATION_TEST ?= smoke
else
TRANSLATION_TEST ?= test_assert
endif

EVALUATION_TEST ?= test_cases
TEST_FILES := $(wildcard test_vectors/*.json)
ifeq ($(LARGE_PROJECT),1)
TARGETS_LIB ?=
TARGETS_BIN ?= $(shell [ -d build-ninja ] && find build-ninja -maxdepth 1 -name '*.sources' ! -name '*.so.sources' -exec basename {} .sources \; )
else
TARGETS_LIB ?= $(shell [ -d build-ninja ] && find build-ninja -maxdepth 1 -name 'lib*.so.sources' -exec basename {} .so.sources \; | sed -e "s/^lib//gi")
TARGETS_BIN ?= $(shell [ -d build-ninja ] && find build-ninja -maxdepth 1 -name '*.sources' ! -name 'lib*.so.sources' -exec basename {} .sources \; )
endif
TARGETS ?= $(TARGETS_BIN) $(TARGETS_LIB)
ifeq (${TARGETS},)
ifeq ($(filter cmake clean,$(MAKECMDGOALS)),)
$(error No TARGETS found! You need to run cmake!)
endif
endif


# cmake
.PHONY: cmake
cmake: build-ninja/cmake.log

build-ninja/cmake.log: test_case/CMakeLists.txt ${EXTRACT_INFO_CMAKE}
	uv run python -m ideas.cmake source_dir=test_case build_dir=build-ninja
	@touch $@

build-ninja/CMakeCache.txt: build-ninja/cmake.log
build-ninja/compile_commands.json: build-ninja/cmake.log
build-ninja/build.log: build-ninja/cmake.log

# init
.PHONY: init
init: $(patsubst %,${TRANSLATION_DIR}/%/init,${TARGETS}) ;
${TRANSLATION_DIR}/%/init: ${TRANSLATION_DIR}/%/build.rs
	touch ${TRANSLATION_DIR}/$*/Cargo.toml
	touch ${TRANSLATION_DIR}/$*/build.rs

# initialize workspace
.PRECIOUS: ${TRANSLATION_DIR}/Cargo.toml
${TRANSLATION_DIR}/Cargo.toml:
	@mkdir -p ${TRANSLATION_DIR}
	uv run python -m ideas.init.workspace cargo_toml=$@ vcs=${VCS}

# initialize translated crate for each C target
# consolidate each C target
# generate build scripts
.PRECIOUS: ${TRANSLATION_DIR}/%/Cargo.toml
.PRECIOUS: ${TRANSLATION_DIR}/%/src/lib.c
.PRECIOUS: ${TRANSLATION_DIR}/%/src/main.c
.PRECIOUS: ${TRANSLATION_DIR}/%/build.rs

${TRANSLATION_DIR}/%/Cargo.toml \
${TRANSLATION_DIR}/%/src/lib.c \
${TRANSLATION_DIR}/%/build.rs: | ${TRANSLATION_DIR}/Cargo.toml build-ninja/compile_commands.json build-ninja/lib%.so.sources
	uv run python -m ideas.init.crate crate_type=lib \
                                  vcs=${VCS} \
                                  hydra.output_subdir=.init.crate \
                                  hydra.run.dir=${TRANSLATION_DIR}/$*
	uv run python -m ideas.init.consolidate filename=build-ninja/compile_commands.json \
                                        vcs=${VCS} \
                                        cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                                        source_priority=build-ninja/lib$*.so.sources \
                                        hydra.output_subdir=.init.consolidate \
                                        hydra.run.dir=${TRANSLATION_DIR}/$*
	uv run python -m ideas.init.build vcs=${VCS} \
                                  hydra.output_subdir=.init.build \
                                  hydra.job.name=init.build \
                                  hydra.run.dir=${TRANSLATION_DIR}/$*

${TRANSLATION_DIR}/%/Cargo.toml \
${TRANSLATION_DIR}/%/src/main.c \
${TRANSLATION_DIR}/%/build.rs: | ${TRANSLATION_DIR}/Cargo.toml build-ninja/compile_commands.json build-ninja/%.sources
	uv run python -m ideas.init.crate crate_type=bin \
                                  vcs=${VCS} \
                                  hydra.output_subdir=.init.crate \
                                  hydra.run.dir=${TRANSLATION_DIR}/$*
	uv run python -m ideas.init.consolidate filename=build-ninja/compile_commands.json \
                                        vcs=${VCS} \
                                        cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                                        source_priority=build-ninja/$*.sources \
                                        hydra.output_subdir=.init.consolidate \
                                        hydra.run.dir=${TRANSLATION_DIR}/$*
	uv run python -m ideas.init.build vcs=${VCS} \
                                  hydra.output_subdir=.init.build \
                                  hydra.job.name=init.build \
                                  hydra.run.dir=${TRANSLATION_DIR}/$*

# translate
.PHONY: translate
translate: $(patsubst %,${TRANSLATION_DIR}/%/translate,${TARGETS}) ;
${TRANSLATION_DIR}/%/translate: ${TRANSLATION_DIR}/%/src/lib.rs | build-ninja/lib%.so.sources ;
${TRANSLATION_DIR}/%/translate: ${TRANSLATION_DIR}/%/src/main.rs | build-ninja/%.sources ;

ifeq ($(LARGE_PROJECT),1)
.PRECIOUS: ${TRANSLATION_DIR}/%/src/lib.rs
${TRANSLATION_DIR}/%/src/lib.rs: | ${TRANSLATION_DIR}/%/Cargo.toml ${TRANSLATION_DIR}/%/tests/${TRANSLATION_TEST}.rs build-ninja/compile_commands.json
	-uv run python -m ideas.translate model.name=${PROVIDER}/${MODEL} \
                                 filename=build-ninja/compile_commands.json \
                                 cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                                 source_priority=build-ninja/lib$*.so.sources \
                                 tests=${TRANSLATION_TEST} \
                                 vcs=${VCS} \
                                 hydra.output_subdir=.translate \
                                 hydra.job.name=translate \
                                 hydra.run.dir=${TRANSLATION_DIR}/$* ${TRANSLATE_ARGS}
	@touch $@

.PRECIOUS: ${TRANSLATION_DIR}/%/src/main.rs
${TRANSLATION_DIR}/%/src/main.rs: | ${TRANSLATION_DIR}/%/Cargo.toml ${TRANSLATION_DIR}/%/tests/${TRANSLATION_TEST}.rs build-ninja/compile_commands.json
	-uv run python -m ideas.translate model.name=${PROVIDER}/${MODEL} \
                                 filename=build-ninja/compile_commands.json \
                                 cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                                 source_priority=build-ninja/$*.sources \
                                 tests=${TRANSLATION_TEST} \
                                 vcs=${VCS} \
                                 hydra.output_subdir=.translate \
                                 hydra.job.name=translate \
                                 hydra.run.dir=${TRANSLATION_DIR}/$* ${TRANSLATE_ARGS}
	@touch $@
else
.PRECIOUS: ${TRANSLATION_DIR}/%/src/lib.rs
${TRANSLATION_DIR}/%/src/lib.rs: ${TRANSLATION_DIR}/%/src/lib.c | ${TRANSLATION_DIR}/%/Cargo.toml ${TRANSLATION_DIR}/%/tests/${TRANSLATION_TEST}.rs
	-uv run python -m ideas.translate model.name=${PROVIDER}/${MODEL} \
                                 filename=${TRANSLATION_DIR}/$*/src/lib.c \
                                 cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                                 tests=${TRANSLATION_TEST} \
                                 vcs=${VCS} \
                                 hydra.output_subdir=.translate \
                                 hydra.job.name=translate \
                                 hydra.run.dir=${TRANSLATION_DIR}/$* ${TRANSLATE_ARGS}
	@touch $@

.PRECIOUS: ${TRANSLATION_DIR}/%/src/main.rs
${TRANSLATION_DIR}/%/src/main.rs: ${TRANSLATION_DIR}/%/src/main.c | ${TRANSLATION_DIR}/%/Cargo.toml ${TRANSLATION_DIR}/%/tests/${TRANSLATION_TEST}.rs
	-uv run python -m ideas.translate model.name=${PROVIDER}/${MODEL} \
                                 filename=${TRANSLATION_DIR}/$*/src/main.c \
                                 cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                                 tests=${TRANSLATION_TEST} \
                                 vcs=${VCS} \
                                 hydra.output_subdir=.translate \
                                 hydra.job.name=translate \
                                 hydra.run.dir=${TRANSLATION_DIR}/$* ${TRANSLATE_ARGS}
	@touch $@
endif

# build
.PHONY: build
build: ${TRANSLATION_DIR}/build.log ;

.PRECIOUS: ${TRANSLATION_DIR}/build.log
${TRANSLATION_DIR}/build.log: $(patsubst %,${TRANSLATION_DIR}/%/build.log,${TARGETS}) ;
	cat $^ > $@

.PRECIOUS: ${TRANSLATION_DIR}/%/build.log
${TRANSLATION_DIR}/%/build.log: ${TRANSLATION_DIR}/%/src/lib.rs
	-export RUSTFLAGS=${RUSTFLAGS} && cargo build --quiet --manifest-path ${TRANSLATION_DIR}/$*/Cargo.toml 2> ${TRANSLATION_DIR}/$*/build.log
	@cat ${TRANSLATION_DIR}/$*/build.log

${TRANSLATION_DIR}/%/build.log: ${TRANSLATION_DIR}/%/src/main.rs
	-export RUSTFLAGS=${RUSTFLAGS} && cargo build --quiet --manifest-path ${TRANSLATION_DIR}/$*/Cargo.toml 2> ${TRANSLATION_DIR}/$*/build.log
	@cat ${TRANSLATION_DIR}/$*/build.log

# test
.PHONY: test
test: ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log ;

.PRECIOUS: ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log
${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log: ${TRANSLATION_DIR}/build.log $(patsubst %,${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log,${TARGETS})
	cat $(filter-out $<,$^) > $@

.PRECIOUS: ${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log
${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log: ${TRANSLATION_DIR}/%/build.log ${TRANSLATION_DIR}/%/tests/${EVALUATION_TEST}.rs | ${TRANSLATION_DIR}/%/Cargo.toml
	uv run python -m ideas.evaluate manifest=${TRANSLATION_DIR}/$*/Cargo.toml \
                                test_cases=${EVALUATION_TEST} \
                                output_file=$@

# convert cando tests
.PRECIOUS: ${TRANSLATION_DIR}/%/tests/test_cases.rs
${TRANSLATION_DIR}/%/tests/test_cases.rs: | ${TEST_FILES} ${TRANSLATION_DIR}/%/Cargo.toml runner/Cargo.toml build-ninja/lib%.so.sources
	uv run python -m ideas.convert_tests runner_manifest=runner/Cargo.toml \
                                     vcs=${VCS} \
                                     template=${MAKEFILE_DIR}/tools/rust_tests/lib_testing.rs \
                                     output=tests/test_cases.rs \
                                     'test_vectors=[$(shell echo "$(TEST_FILES)" | tr ' ' ',')]' \
                                     hydra.output_subdir=.convert_tests \
                                     hydra.run.dir=${TRANSLATION_DIR}/$*

${TRANSLATION_DIR}/%/tests/test_cases.rs: | ${TEST_FILES} ${TRANSLATION_DIR}/%/Cargo.toml build-ninja/%.sources
	uv run python -m ideas.convert_tests vcs=${VCS} \
                                     output=tests/test_cases.rs \
                                     'test_vectors=[$(shell echo "$(TEST_FILES)" | tr ' ' ',')]' \
                                     hydra.output_subdir=.convert_tests \
                                     hydra.run.dir=${TRANSLATION_DIR}/$*

# can't rely on test vectors without explicit targets
.PRECIOUS: test_vectors/%.json
test_vectors/%.json:
	$(error $@ not found)

.PRECIOUS: test_vectors/%/%.json
test_vectors/%/%.json:
	$(error $@ not found)


# testgen for each C target
.PRECIOUS: test_crates/%/tests/test_assert.rs
test_crates/%/tests/test_assert.rs: | build-ninja/lib%.so.sources
	-@$(MAKE) -j1 -f $(AGENTS_MAKEFILE) $@

test_crates/%/tests/test_assert.rs: | build-ninja/%.sources
	-@$(MAKE) -j1 -f $(AGENTS_MAKEFILE) $@

.PRECIOUS: ${TRANSLATION_DIR}/%/tests/test_assert.rs
${TRANSLATION_DIR}/%/tests/test_assert.rs: test_crates/%/tests/test_assert.rs
	mkdir -p $(dir $@)
	cp $< $@

# test wrappers instead of bindings
.PRECIOUS: ${TRANSLATION_DIR}/%/tests/test_assert_wrapper.rs
${TRANSLATION_DIR}/%/tests/test_assert_wrapper.rs: test_crates/%/tests/test_assert.rs
	cat $< | sed 's/$*::binding::/$*::wrapper::/g' > $@

# smoke test
.PRECIOUS: ${TRANSLATION_DIR}/%/tests/smoke.rs
${TRANSLATION_DIR}/%/tests/smoke.rs:
	mkdir -p $(dir $@)
	echo "#[test]" >> $@
	echo "fn smoke() {" >> $@
	echo "    assert_eq!(1, 1);" >> $@
	echo "}" >> $@


# clean
.PHONY: clean
clean:
	rm -rf build-ninja
	find . -name Cargo.toml -exec cargo clean --quiet --manifest-path {} \;
