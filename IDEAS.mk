#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

.NOTPARALLEL:
MAKEFILE_DIR := $(realpath $(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
include ${MAKEFILE_DIR}/VARIABLES.mk
include ${MAKEFILE_DIR}/INSTALL.mk
include ${MAKEFILE_DIR}/docker/DOCKER.mk

GIT := @git -C ${TRANSLATION_DIR}
CMAKE := cmake
CARGO := cargo -q
PYTHON := uv run python

ifeq (${TARGETS},)
ifeq ($(filter bear clean reset,$(MAKECMDGOALS)),)
$(error No TARGETS found! You need to run bear!)
endif
endif

TEST_FILES := $(wildcard test_vectors/*.json)
ifeq ($(filter-out null,$(strip ${TRANSLATION_TEST})),)
override TRANSLATION_TEST := null
TRANSLATION_TEST_ARG := null
TRANSLATION_TEST_DEP :=
else
TRANSLATION_TEST_ARG = ${TESTGEN_CACHE}/$*-sys/tests/${TRANSLATION_TEST}.rs
TRANSLATION_TEST_DEP = ${TESTGEN_CACHE}/%-sys/tests/${TRANSLATION_TEST}.rs
endif

# bear
.PHONY: bear
bear: build-ninja/events.jsonl
	@[ -s build-ninja/events.jsonl ] || (echo "BROKEN ${CURDIR}/${TRANSLATION_DIR}")
	@:  # empty rule to suppress "Nothing to be done for"
.DELETE_ON_ERROR: build-ninja/events.jsonl
ifneq ($(wildcard CMakePresets.json),)
build-ninja/events.jsonl: CMakeLists.txt CMakePresets.json
	@rm -rf build-ninja
	${CMAKE} -S $(<D) -B $(@D) -G Ninja -DCMAKE_C_COMPILER=clang --preset test
	${PYTHON} -m ideas.bear --output-dir $(@D) -- ${CMAKE} --build $(@D) --target all --preset test
else
build-ninja/events.jsonl: test_case/CMakeLists.txt
	@rm -rf build-ninja
	${CMAKE} -S $(<D) -B $(@D) -G Ninja -DCMAKE_C_COMPILER=clang
	${PYTHON} -m ideas.bear --output-dir $(@D) -- ${CMAKE} --build $(@D) --target all
endif

# baseline
.PHONY: baseline
baseline: build-ninja/baseline.json ;
.PRECIOUS: build-ninja/baseline.json
build-ninja/baseline.json: | build-ninja/events.jsonl
ifneq ($(wildcard runner/Cargo.toml),)
build-ninja/baseline.json:
	RUSTUP_TOOLCHAIN="${RUNNER_TOOLCHAIN}" CARGO_TARGET_DIR=build-ninja/cargo \
    ${CARGO} run --manifest-path runner/Cargo.toml --release \
                 -- \
                 --log-level quiet lib > $@
else
build-ninja/baseline.json:
	RUSTUP_TOOLCHAIN="${RUNNER_TOOLCHAIN}" CARGO_TARGET_DIR=build-ninja/cargo \
    ${CARGO} run --manifest-path ${MAKEFILE_DIR}/tools/cando2/Cargo.toml --release --bin cando_binrunner \
                 -- \
                 --log-level quiet bin --name ${TARGETS_BIN} > $@
endif


# workspace
${TRANSLATION_DIR}/.git/config:
	@mkdir -p ${TRANSLATION_DIR}
	${GIT} init --initial-branch=${TRANSLATION_BRANCH} --quiet
	${GIT} config core.excludesFile /dev/null

define WORKSPACE_CARGO_TOML
[workspace]
resolver = "3"

[workspace.dependencies]
libc = "0.2.185"
openssl = "0.10.79"
regex = "1"
flate2 = "1"
insta = { version = "1.48.0", features = ["json"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tempfile = "3"
assert_cmd = "2.0.17"
predicates = "3.1.3"
walkdir = "2"
endef
define WORKSPACE_GITIGNORE
cache.db*
target/
*.log
endef
.PRECIOUS: ${TRANSLATION_DIR}/Cargo.toml
${TRANSLATION_DIR}/Cargo.toml: | ${TRANSLATION_DIR}/.git/config
	$(file >$@,${WORKSPACE_CARGO_TOML})
	$(file >$(@D)/.gitignore,${WORKSPACE_GITIGNORE})
	${GIT} add Cargo.toml .gitignore
	${GIT} commit -qm "Created cargo workspace"
	@echo ""


# init
.PHONY: init
.PRECIOUS: ${TRANSLATION_DIR}/%-sys/Cargo.toml ${TRANSLATION_DIR}/%-sys/src/lib.c
init: $(patsubst %,${TRANSLATION_DIR}/%-sys/Cargo.toml,${TARGETS})
	touch $(patsubst %,${TRANSLATION_DIR}/%-sys/Cargo.toml,${TARGETS}) \
      $(patsubst %,${TRANSLATION_DIR}/%-sys/src/lib.c,${TARGETS})
${TRANSLATION_DIR}/%-sys/src/lib.c ${TRANSLATION_DIR}/%-sys/Cargo.toml &: | ${TRANSLATION_DIR}/Cargo.toml build-ninja/%.so.d/compile_commands.json
	${PYTHON} -m ideas.consolidate cargo_toml=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
                                   template=lib \
                                   compile_commands=build-ninja/$*.so.d/compile_commands.json \
                                   links=build-ninja/$*.so.d/links.json \
                                   vcs=${VCS} \
                                   hydra.output_subdir=.consolidate \
                                   hydra.job.name=consolidate \
                                   hydra.run.dir=${TRANSLATION_DIR}/$*-sys
	@touch ${TRANSLATION_DIR}/$*-sys/Cargo.toml ${TRANSLATION_DIR}/$*-sys/src/lib.c
	@echo ""
${TRANSLATION_DIR}/%-sys/src/lib.c ${TRANSLATION_DIR}/%-sys/Cargo.toml &: | ${TRANSLATION_DIR}/Cargo.toml build-ninja/%.d/compile_commands.json
	${PYTHON} -m ideas.consolidate cargo_toml=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
                                   compile_commands=build-ninja/$*.d/compile_commands.json \
                                   links=build-ninja/$*.d/links.json \
                                   vcs=${VCS} \
                                   hydra.output_subdir=.consolidate \
                                   hydra.job.name=consolidate \
                                   hydra.run.dir=${TRANSLATION_DIR}/$*-sys
	@touch ${TRANSLATION_DIR}/$*-sys/Cargo.toml ${TRANSLATION_DIR}/$*-sys/src/lib.c
	@echo ""


# translate
.PHONY: translate
.PRECIOUS: ${TRANSLATION_DIR}/%/Cargo.toml ${TRANSLATION_DIR}/%/src/lib.rs ${TRANSLATION_DIR}/%/src/main.rs
SELECTED_TARGETS_LIB := $(filter ${TARGETS},${TARGETS_LIB})
SELECTED_TARGETS_BIN := $(filter ${TARGETS},${TARGETS_BIN})
SELECTED_TARGET_OUTPUTS := $(patsubst %,${TRANSLATION_DIR}/%/src/lib.rs,${SELECTED_TARGETS_LIB})
SELECTED_TARGET_OUTPUTS += $(patsubst %,${TRANSLATION_DIR}/%/src/main.rs,${SELECTED_TARGETS_BIN})
translate: $(patsubst %,${TRANSLATION_DIR}/%/Cargo.toml,${TARGETS})
	@touch $(patsubst %,${TRANSLATION_DIR}/%/Cargo.toml,${TARGETS}) ${SELECTED_TARGET_OUTPUTS}
${TRANSLATION_DIR}/%/src/lib.rs ${TRANSLATION_DIR}/%/src/main.rs ${TRANSLATION_DIR}/%/Cargo.toml: private RUN_ARGS += --writable "${CURDIR}/${TRANSLATION_DIR}"
ifneq ($(filter-out null,$(strip ${TRANSLATION_TEST})),)
ifneq ($(abspath ${TESTGEN_CACHE}),$(abspath ${TRANSLATION_DIR}))
${TRANSLATION_DIR}/%/src/lib.rs ${TRANSLATION_DIR}/%/src/main.rs ${TRANSLATION_DIR}/%/Cargo.toml: private RUN_ARGS += --readable "${CURDIR}/${TESTGEN_CACHE}"
endif
endif
${TRANSLATION_DIR}/%/src/lib.rs ${TRANSLATION_DIR}/%/Cargo.toml &: ${TRANSLATION_DIR}/%-sys/src/lib.c | ${TRANSLATION_DIR}/%-sys/Cargo.toml ${TRANSLATION_TEST_DEP} build-ninja/%.so.d/compile_commands.json
	-${DOCKER_RUN} ${PYTHON} -m ideas.translate cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                                                bindings_cargo_toml=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
                                                template=lib \
                                                vcs=${VCS} \
                                                tests=${TRANSLATION_TEST_ARG} \
                                                'deps=[openssl,flate2,regex]' \
                                                model.name=${PROVIDER}/${MODEL} \
                                                model.reasoning_effort=${REASONING_EFFORT} \
                                                hydra.output_subdir=.translate \
                                                hydra.job.name=translate \
                                                hydra.run.dir=${TRANSLATION_DIR}/$* ${TRANSLATE_ARGS}
	@touch ${TRANSLATION_DIR}/$*/Cargo.toml ${TRANSLATION_DIR}/$*/src/lib.rs
	@echo ""
${TRANSLATION_DIR}/%/src/main.rs ${TRANSLATION_DIR}/%/Cargo.toml &: ${TRANSLATION_DIR}/%-sys/src/lib.c | ${TRANSLATION_DIR}/%-sys/Cargo.toml ${TRANSLATION_TEST_DEP} build-ninja/%.d/compile_commands.json
	-${DOCKER_RUN} ${PYTHON} -m ideas.translate cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                                                bindings_cargo_toml=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
                                                vcs=${VCS} \
                                                tests=${TRANSLATION_TEST_ARG} \
                                                'deps=[openssl,flate2,regex]' \
                                                model.name=${PROVIDER}/${MODEL} \
                                                model.reasoning_effort=${REASONING_EFFORT} \
                                                hydra.output_subdir=.translate \
                                                hydra.job.name=translate \
                                                hydra.run.dir=${TRANSLATION_DIR}/$* ${TRANSLATE_ARGS}
	@touch ${TRANSLATION_DIR}/$*/Cargo.toml ${TRANSLATION_DIR}/$*/src/main.rs
	@echo ""


# cost
.PHONY: cost
cost: ${TRANSLATION_DIR}/cost.tsv
	@:  # empty rule to suppress "Nothing to be done for"
${TRANSLATION_DIR}/cost.tsv: $(patsubst %,${TRANSLATION_DIR}/%/cost.tsv,${TARGETS})
	@cat $^ | sort -k1 > $@
${TRANSLATION_DIR}/%/cost.tsv: ${TRANSLATION_DIR}/%/translate.log
	@cat $^ | awk 'match($$0,/^\[[^]]+\]\[([^]]+)\]\[[^]]+\].*`([^`]+)`[^$$]*([$$][0-9.]+), ([0-9,]+) tok \(([0-9,]+) in \/ ([0-9,]+) out\)/,m){printf "%s\t%s\t%s\t%s\t%s\t%s\n",m[1],m[2],m[3],m[4],m[5],m[6]}' | sort -k1,2 > $@


# build
.PHONY: build
.PRECIOUS: ${TRANSLATION_DIR}/build.log ${TRANSLATION_DIR}/%/build.log
build: ${TRANSLATION_DIR}/build.log
	@:  # empty rule to suppress "Nothing to be done for"
${TRANSLATION_DIR}/build.log: $(patsubst %,${TRANSLATION_DIR}/%/build.log,${TARGETS})
	@cat $^ > $@
${TRANSLATION_DIR}/%/build.log: | ${TRANSLATION_DIR}/%/Cargo.toml
	-${CARGO} build --manifest-path ${TRANSLATION_DIR}/Cargo.toml -p $* 2> ${TRANSLATION_DIR}/$*/build.log
	@cat ${TRANSLATION_DIR}/$*/build.log


# test
.PHONY: test
.PRECIOUS: ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log ${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log
.PRECIOUS: ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl ${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.jsonl
test: ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl ${TRANSLATION_DIR}/build.log
	@:  # empty rule to suppress "Nothing to be done for"
${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log: $(patsubst %,${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log,${TARGETS})
	@cat $^ > $@
${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl: $(patsubst %,${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.jsonl,${TARGETS})
	@cat $^ > $@
${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log ${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.jsonl &: ${TRANSLATION_DIR}/%/build.log ${TRANSLATION_DIR}/%/tests/${EVALUATION_TEST}.rs
	-${CARGO} nextest run --manifest-path ${TRANSLATION_DIR}/Cargo.toml -p $* --test ${EVALUATION_TEST} \
                          --message-format=libtest-json \
                          --no-fail-fast --no-tests=pass \
                          --color=always \
                          --success-output=never --failure-output=never \
                          --status-level=none --final-status-level=all \
                          > ${TRANSLATION_DIR}/$*/cargo_${EVALUATION_TEST}.jsonl \
                          2> ${TRANSLATION_DIR}/$*/cargo_${EVALUATION_TEST}.log

# convert cando tests
.PRECIOUS: ${TRANSLATION_DIR}/%/tests/test_cases.rs
${TRANSLATION_DIR}/%/tests/test_cases.rs: | ${TEST_FILES} ${TRANSLATION_DIR}/%/Cargo.toml runner/Cargo.toml build-ninja/%.so.d/compile_commands.json
	${PYTHON} -m ideas.convert_tests runner_manifest=runner/Cargo.toml \
                                     vcs=${VCS} \
                                     template=${MAKEFILE_DIR}/tools/rust_tests/lib_testing.rs \
                                     output=tests/test_cases.rs \
                                     'test_vectors=[$(shell echo "$(TEST_FILES)" | tr ' ' ',')]' \
                                     hydra.output_subdir=.convert_tests \
                                     hydra.run.dir=${TRANSLATION_DIR}/$*
${TRANSLATION_DIR}/%/tests/test_cases.rs: | ${TEST_FILES} ${TRANSLATION_DIR}/%/Cargo.toml build-ninja/%.d/compile_commands.json
	${PYTHON} -m ideas.convert_tests vcs=${VCS} \
                                     output=tests/test_cases.rs \
                                     'test_vectors=[$(shell echo "$(TEST_FILES)" | tr ' ' ',')]' \
                                     hydra.output_subdir=.convert_tests \
                                     hydra.run.dir=${TRANSLATION_DIR}/$*

# generate and run I/O equivalence tests
.PHONY: testgen
.PRECIOUS: ${TRANSLATION_DIR}/cargo_io.jsonl
.PRECIOUS: ${TRANSLATION_DIR}/cargo_io.log
testgen: ${TRANSLATION_DIR}/cargo_io.jsonl ${TRANSLATION_DIR}/cargo_io.log
	@:  # empty rule to suppress "Nothing to be done for"
${TRANSLATION_DIR}/cargo_io.jsonl: $(patsubst %,${TRANSLATION_DIR}/%-sys/cargo_io.jsonl,${TARGETS})
	@cat $^ > $@
${TRANSLATION_DIR}/cargo_io.log: $(patsubst %,${TRANSLATION_DIR}/%-sys/cargo_io.log,${TARGETS})
	@cat $^ > $@
.PRECIOUS: ${TRANSLATION_DIR}/%-sys/tests/io.rs
${TRANSLATION_DIR}/%-sys/tests/io.rs: private RUN_ARGS += --writable "${CURDIR}/${TRANSLATION_DIR}"
${TRANSLATION_DIR}/%-sys/tests/io.rs: | ${TRANSLATION_DIR}/%-sys/Cargo.toml
	${DOCKER_RUN} ${PYTHON} -m ideas.agents.generate_io_tests model=${PROVIDER}/${MODEL} \
                                                              manifest=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
                                                              budget=${TESTGEN_BUDGET} \
                                                              steps=${TESTGEN_STEPS} \
                                                              coverage=${TESTGEN_COVERAGE} \
                                                              hydra.output_subdir=.generate_io_tests \
                                                              hydra.job.name=generate_io_tests \
                                                              hydra.run.dir=${TRANSLATION_DIR}/$*-sys
.PRECIOUS: ${TRANSLATION_DIR}/%-sys/cargo_io.jsonl ${TRANSLATION_DIR}/%-sys/cargo_io.log
${TRANSLATION_DIR}/%-sys/cargo_io.jsonl ${TRANSLATION_DIR}/%-sys/cargo_io.log &: ${TRANSLATION_DIR}/%-sys/Cargo.toml ${TRANSLATION_DIR}/%-sys/tests/io.rs
	-${CARGO} nextest run --manifest-path $< --test io \
                          --message-format=libtest-json \
                          --no-fail-fast \
                          --test-threads 1 \
                          > ${TRANSLATION_DIR}/$*-sys/cargo_io.jsonl \
                          2> ${TRANSLATION_DIR}/$*-sys/cargo_io.log

# write smoke tests
.PRECIOUS: ${TESTGEN_CACHE}/%-sys/tests/smoke.rs
${TESTGEN_CACHE}/%-sys/tests/smoke.rs:
	@mkdir -p $(@D)
	@printf '#[test]\nfn smoke() {\n    assert_eq!(1, 1);\n}\n' > $@

# reset
.PHONY: reset
ifneq ($(wildcard ${TRANSLATION_DIR}/.git),)
reset:
	${GIT} add -A
	-${GIT} add -f -- '*.log' 2>/dev/null
	${GIT} commit -q --allow-empty -m "Checkpoint before reset to ${TRANSLATION_BRANCH}"
	${GIT} switch --orphan ${TRANSLATION_BRANCH}
	${GIT} clean -xdfq -e '/cache.db*'
else
reset:
	@echo "Skipped reset of ${TRANSLATION_DIR}"
endif


# clean
.PHONY: clean
clean:
	rm -rf build-ninja
ifneq ($(wildcard ${TRANSLATION_DIR}/Cargo.toml),)
	${CARGO} clean --manifest-path ${TRANSLATION_DIR}/Cargo.toml
endif
