#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

.NOTPARALLEL:
MAKEFILE_DIR := $(realpath $(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
include ${MAKEFILE_DIR}/VARIABLES.mk

TEST_FILES := $(wildcard test_vectors/*.json)
GIT := @git -C ${TRANSLATION_DIR}
CARGO := cargo -q
PYTHON := uv run python

ifeq (${TARGETS},)
ifeq ($(filter bear clean,$(MAKECMDGOALS)),)
$(error No TARGETS found! You need to run bear!)
endif
endif

# TRANSLATION_TEST=null (or empty) disables translation-time testing: no test file is
# required from the -sys crate and RecurrentTranslator only wraps global symbols. Empty
# is normalized to null because hydra parses a bare `tests=` as "" rather than None.
ifeq ($(filter-out null,$(strip ${TRANSLATION_TEST})),)
override TRANSLATION_TEST := null
TRANSLATION_TEST_DEP :=
else
# Recursive `=` so `%` stays literal until the pattern rule substitutes the stem
TRANSLATION_TEST_DEP = ${TRANSLATION_DIR}/%-sys/tests/${TRANSLATION_TEST}.rs
endif

# bear
.PHONY: bear
bear: build-ninja/events.jsonl
	@[ -s build-ninja/events.jsonl ] || (echo "BROKEN ${CURDIR}/${TRANSLATION_DIR}")
	@:  # empty rule to suppress "Nothing to be done for"

ifneq ($(wildcard CMakePresets.json),)
build-ninja/events.jsonl: CMakeLists.txt CMakePresets.json
	rm -rf $(@D)
	cmake -S $(<D) -B $(@D) -G Ninja -DCMAKE_C_COMPILER=clang --preset test
	${PYTHON} -m ideas.bear --output-dir $(@D) -- cmake --build $(@D) --target all --preset test
else
build-ninja/events.jsonl: test_case/CMakeLists.txt
	rm -rf $(@D)
	cmake -S $(<D) -B $(@D) -G Ninja -DCMAKE_C_COMPILER=clang
	${PYTHON} -m ideas.bear --output-dir $(@D) -- cmake --build $(@D) --target all
endif


# workspace
${TRANSLATION_DIR}/.git/config:
	@mkdir -p ${TRANSLATION_DIR}
	${GIT} init --initial-branch=main --quiet

define WORKSPACE_CARGO_TOML
[workspace]
resolver = "3"

[workspace.dependencies]
libc = "0.2.185"
openssl = "0.10.79"
regex = "1"
flate2 = "1"
serde = { version = "1", features = ["derive"] }
serde_json = "1"
tempfile = "3"
assert_cmd = "2.0.17"
predicates = "3.1.3"
endef
.PRECIOUS: ${TRANSLATION_DIR}/Cargo.toml
${TRANSLATION_DIR}/Cargo.toml: | ${TRANSLATION_DIR}/.git/config
	$(file >$@,${WORKSPACE_CARGO_TOML})
	${GIT} add $(@F)
	${GIT} commit -qm "Created cargo workspace"
	@echo ""


# init
.PHONY: init
.PRECIOUS: ${TRANSLATION_DIR}/%-sys/Cargo.toml ${TRANSLATION_DIR}/%-sys/src/lib.c
init: $(patsubst %,${TRANSLATION_DIR}/%-sys/Cargo.toml,${TARGETS})
	touch $(patsubst %,${TRANSLATION_DIR}/%-sys/Cargo.toml,${TARGETS}) \
      $(patsubst %,${TRANSLATION_DIR}/%-sys/src/lib.c,${TARGETS})
${TRANSLATION_DIR}/%-sys/src/lib.c \
${TRANSLATION_DIR}/%-sys/Cargo.toml &: | ${TRANSLATION_DIR}/Cargo.toml build-ninja/%.so.d/compile_commands.json
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
${TRANSLATION_DIR}/%-sys/src/lib.c \
${TRANSLATION_DIR}/%-sys/Cargo.toml &: | ${TRANSLATION_DIR}/Cargo.toml build-ninja/%.d/compile_commands.json
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
translate: $(patsubst %,${TRANSLATION_DIR}/%/Cargo.toml,${TARGETS})
	touch $(patsubst %,${TRANSLATION_DIR}/%/Cargo.toml,${TARGETS}) \
      $(patsubst %,${TRANSLATION_DIR}/%/src/lib.rs,${TARGETS_LIB}) $(patsubst %,${TRANSLATION_DIR}/%/src/main.rs,${TARGETS_BIN})
${TRANSLATION_DIR}/%/src/lib.rs \
${TRANSLATION_DIR}/%/Cargo.toml &: ${TRANSLATION_DIR}/%-sys/src/lib.c | ${TRANSLATION_DIR}/%-sys/Cargo.toml ${TRANSLATION_TEST_DEP} build-ninja/%.so.d/compile_commands.json
	-${PYTHON} -m ideas.translate cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                                 bindings_cargo_toml=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
                                 template=lib \
                                 vcs=${VCS} \
                                 tests=${TRANSLATION_TEST} \
                                 'deps=[libc,openssl,flate2,regex]' \
                                 model.name=${PROVIDER}/${MODEL} \
                                 hydra.output_subdir=.translate \
                                 hydra.job.name=translate \
                                 hydra.run.dir=${TRANSLATION_DIR}/$* ${TRANSLATE_ARGS}
	@touch ${TRANSLATION_DIR}/$*/Cargo.toml ${TRANSLATION_DIR}/$*/src/lib.rs
	@echo ""
${TRANSLATION_DIR}/%/src/main.rs \
${TRANSLATION_DIR}/%/Cargo.toml &: ${TRANSLATION_DIR}/%-sys/src/lib.c | ${TRANSLATION_DIR}/%-sys/Cargo.toml ${TRANSLATION_TEST_DEP} build-ninja/%.d/compile_commands.json
	-${PYTHON} -m ideas.translate cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                                 bindings_cargo_toml=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
                                 vcs=${VCS} \
                                 tests=${TRANSLATION_TEST} \
                                 'deps=[libc,openssl,flate2,regex]' \
                                 model.name=${PROVIDER}/${MODEL} \
                                 hydra.output_subdir=.translate \
                                 hydra.job.name=translate \
                                 hydra.run.dir=${TRANSLATION_DIR}/$* ${TRANSLATE_ARGS}
	@touch ${TRANSLATION_DIR}/$*/Cargo.toml ${TRANSLATION_DIR}/$*/src/main.rs
	@echo ""


# cost
.PHONY: cost
cost: ${TRANSLATION_DIR}/cost.tsv
ifneq (${VERBOSE},0)
	@echo "# ${CURDIR}/${TRANSLATION_DIR}"
	@cat $^ | tr -d '$$,' | datamash -g1 sum 3 sum 4 sum 5 sum 6 | awk '{printf "%28s $$%10.4f %12\047d tok (%12\047d in / %12\047d out)\n",$$1,$$2,$$3,$$4,$$5}'
	@echo ""
else
	@:
endif

${TRANSLATION_DIR}/cost.tsv: $(patsubst %,${TRANSLATION_DIR}/%/cost.tsv,${TARGETS})
	@cat $^ | sort -k1 > $@

${TRANSLATION_DIR}/%/cost.tsv: ${TRANSLATION_DIR}/%/translate.log
	@cat $^ | awk 'match($$0,/^\[[^]]+\]\[([^]]+)\]\[[^]]+\].*`([^`]+)`[^$$]*([$$][0-9.]+), ([0-9,]+) tok \(([0-9,]+) in \/ ([0-9,]+) out\)/,m){printf "%s\t%s\t%s\t%s\t%s\t%s\n",m[1],m[2],m[3],m[4],m[5],m[6]}' | sort -k1,2 > $@


# build
.PHONY: build
.PRECIOUS: ${TRANSLATION_DIR}/build.log ${TRANSLATION_DIR}/%/build.log
build: ${TRANSLATION_DIR}/build.log
	@:
${TRANSLATION_DIR}/build.log: $(patsubst %,${TRANSLATION_DIR}/%/build.log,${TARGETS}) ;
	@cat $^ > $@
${TRANSLATION_DIR}/%/build.log: | ${TRANSLATION_DIR}/%/Cargo.toml
	-export RUSTFLAGS=${RUSTFLAGS} && cd ${TRANSLATION_DIR} && ${CARGO} build -p $* 2> $*/build.log
	@cat ${TRANSLATION_DIR}/$*/build.log


# test
.PHONY: test
.PRECIOUS: ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log ${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log
.PRECIOUS: ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl ${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.jsonl
test: ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl ${TRANSLATION_DIR}/build.log
	@:
${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log: $(patsubst %,${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log,${TARGETS})
	@cat $^ > $@
${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl: $(patsubst %,${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.jsonl,${TARGETS})
	@cat $^ > $@
${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log \
${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.jsonl &: ${TRANSLATION_DIR}/%/build.log ${TRANSLATION_DIR}/%/tests/${EVALUATION_TEST}.rs ${TRANSLATION_DIR}/%-sys/tests/${EVALUATION_TEST}.rs
	-export RUSTFLAGS=${RUSTFLAGS} NEXTEST_EXPERIMENTAL_LIBTEST_JSON=1 && \
      cd ${TRANSLATION_DIR} && ${CARGO} nextest run -p $* --test ${EVALUATION_TEST} --message-format=libtest-json --no-fail-fast --cargo-quiet --color=always --success-output=never --failure-output=never --status-level=none --final-status-level=all --no-tests=pass > $*/cargo_${EVALUATION_TEST}.jsonl 2> $*/cargo_${EVALUATION_TEST}.log

# convert cando tests
# FIXME: We should just copy tests and deps from the -sys crate!
.PRECIOUS: ${TRANSLATION_DIR}/%/tests/test_cases.rs ${TRANSLATION_DIR}/%-sys/tests/test_cases.rs
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
${TRANSLATION_DIR}/%-sys/tests/test_cases.rs: | ${TEST_FILES} ${TRANSLATION_DIR}/%-sys/Cargo.toml runner/Cargo.toml build-ninja/%.so.d/compile_commands.json
	${PYTHON} -m ideas.convert_tests runner_manifest=runner/Cargo.toml \
                                     vcs=${VCS} \
                                     template=${MAKEFILE_DIR}/tools/rust_tests/lib_testing.rs \
                                     output=tests/test_cases.rs \
                                     'test_vectors=[$(shell echo "$(TEST_FILES)" | tr ' ' ',')]' \
                                     hydra.output_subdir=.convert_tests \
                                     hydra.run.dir=${TRANSLATION_DIR}/$*-sys
${TRANSLATION_DIR}/%-sys/tests/test_cases.rs: | ${TEST_FILES} ${TRANSLATION_DIR}/%-sys/Cargo.toml build-ninja/%.d/compile_commands.json
	${PYTHON} -m ideas.convert_tests vcs=${VCS} \
                                     output=tests/test_cases.rs \
                                     'test_vectors=[$(shell echo "$(TEST_FILES)" | tr ' ' ',')]' \
                                     hydra.output_subdir=.convert_tests \
                                     hydra.run.dir=${TRANSLATION_DIR}/$*-sys


# can't rely on test vectors without explicit targets
.PRECIOUS: test_vectors/%.json
test_vectors/%.json:
	$(error $@ not found)

# generate I/O equivalence tests
.PHONY: testgen
.PRECIOUS: ${TRANSLATION_DIR}/%-sys/tests/io.rs
${TRANSLATION_DIR}/%-sys/tests/io.rs:
testgen: $(patsubst %,${TRANSLATION_DIR}/%-sys/tests/io.rs,${TARGETS})
	touch $(patsubst %,${TRANSLATION_DIR}/%-sys/tests/io.rs,${TARGETS})

${TRANSLATION_DIR}/%-sys/tests/io.rs: | ${TRANSLATION_DIR}/%-sys/Cargo.toml
	${PYTHON} -m ideas.agents.generate_io_tests model=${PROVIDER}/${MODEL} \
                                      manifest=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
                                      budget=${TESTGEN_BUDGET} \
                                      coverage=${TESTGEN_COVERAGE} \
                                      hydra.output_subdir=.generate_io_tests \
                                      hydra.job.name=generate_io_tests \
                                      hydra.run.dir=${TRANSLATION_DIR}/$*-sys


# write smoke tests
.PRECIOUS: ${TRANSLATION_DIR}/%-sys/tests/smoke.rs
${TRANSLATION_DIR}/%-sys/tests/smoke.rs:
	@mkdir -p $(@D)
	@printf '#[test]\nfn smoke() {\n    assert_eq!(1, 1);\n}\n' > $@
	${GIT} add $*-sys/tests/smoke.rs
	${GIT} commit -qm "Generated smoke tests"


# clean
.PHONY: clean
clean:
	rm -rf build-ninja
	find . -name Cargo.toml -exec cargo clean --quiet --manifest-path {} \;
