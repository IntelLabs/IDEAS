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

# The crates that ship each target: lib<name>.so from -ffi, the binary from -rs
SHIPPED_CRATES := $(patsubst %,%-ffi,${TARGETS_LIB}) $(patsubst %,%-rs,${TARGETS_BIN})
# The crates already on disk, for phases that must not start a translation
EXISTING_CRATES := $(patsubst ${TRANSLATION_DIR}/%/Cargo.toml,%,$(wildcard ${TRANSLATION_DIR}/*/Cargo.toml))

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
build-ninja/events.jsonl: private RUN_ARGS += --writable "${CURDIR}"
ifneq ($(wildcard CMakePresets.json),)
build-ninja/events.jsonl: CMakeLists.txt CMakePresets.json
	@rm -rf build-ninja
	${DOCKER_RUN} ${CMAKE} -S $(<D) -B $(@D) -G Ninja -DCMAKE_C_COMPILER=clang --preset test
	${DOCKER_RUN} ${PYTHON} -m ideas.bear --output-dir $(@D) -- ${CMAKE} --build $(@D) --target all --preset test
else
build-ninja/events.jsonl: test_case/CMakeLists.txt
	@rm -rf build-ninja
	${DOCKER_RUN} ${CMAKE} -S $(<D) -B $(@D) -G Ninja -DCMAKE_C_COMPILER=clang
	${DOCKER_RUN} ${PYTHON} -m ideas.bear --output-dir $(@D) -- ${CMAKE} --build $(@D) --target all
endif

# baseline
.PHONY: baseline
baseline: build-ninja/baseline.json ;
.PRECIOUS: build-ninja/baseline.json
build-ninja/baseline.json: | build-ninja/events.jsonl
build-ninja/baseline.json: private RUN_ARGS += --writable "${CURDIR}"
ifneq ($(wildcard runner/Cargo.toml),)
build-ninja/baseline.json:
	${DOCKER_RUN} env RUSTUP_TOOLCHAIN=${RUNNER_TOOLCHAIN} \
        ${CARGO} run --manifest-path runner/Cargo.toml --release \
                     -- \
                     --log-level quiet lib > $@
else
build-ninja/baseline.json:
	${DOCKER_RUN} cando_binrunner --log-level quiet bin --name ${TARGETS_BIN} > $@
endif


# workspace
${TRANSLATION_DIR}/.git/config:
	@mkdir -p ${TRANSLATION_DIR}
	${GIT} init --initial-branch=${TRANSLATION_BRANCH} --quiet
	${GIT} config core.excludesFile /dev/null

.PRECIOUS: ${TRANSLATION_DIR}/Cargo.toml
${TRANSLATION_DIR}/Cargo.toml: | ${MAKEFILE_DIR}/docker/templates/workspace/Cargo.toml ${TRANSLATION_DIR}/.git/config
	@sed '/^members = \["example"\]$$/d' ${MAKEFILE_DIR}/docker/templates/workspace/Cargo.toml > $@
	@cp ${MAKEFILE_DIR}/docker/templates/workspace/Cargo.lock ${TRANSLATION_DIR}/Cargo.lock
	@cp ${MAKEFILE_DIR}/docker/templates/workspace/.gitignore ${TRANSLATION_DIR}/.gitignore
	${GIT} add Cargo.toml Cargo.lock .gitignore
	${GIT} commit -qm "Created cargo workspace"
	@echo ""


# init
.PHONY: init
.PRECIOUS: ${TRANSLATION_DIR}/%-sys/Cargo.toml ${TRANSLATION_DIR}/%-sys/src/lib.c
init: $(patsubst %,${TRANSLATION_DIR}/%-sys/Cargo.toml,${TARGETS})
	touch $(patsubst %,${TRANSLATION_DIR}/%-sys/Cargo.toml,${TARGETS}) \
      $(patsubst %,${TRANSLATION_DIR}/%-sys/src/lib.c,${TARGETS})
${TRANSLATION_DIR}/%-sys/src/lib.c ${TRANSLATION_DIR}/%-sys/Cargo.toml &: private RUN_ARGS += --writable "${CURDIR}"
${TRANSLATION_DIR}/%-sys/src/lib.c ${TRANSLATION_DIR}/%-sys/Cargo.toml &: | ${TRANSLATION_DIR}/Cargo.toml build-ninja/%.so.d/compile_commands.json
	${DOCKER_RUN} ${PYTHON} -m ideas.consolidate cargo_toml=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
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
	${DOCKER_RUN} ${PYTHON} -m ideas.consolidate cargo_toml=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
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
.PRECIOUS: ${TRANSLATION_DIR}/%-ffi/Cargo.toml ${TRANSLATION_DIR}/%-ffi/src/lib.rs
.PRECIOUS: ${TRANSLATION_DIR}/%-rs/Cargo.toml ${TRANSLATION_DIR}/%-rs/src/main.rs
translate: $(patsubst %,${TRANSLATION_DIR}/%/Cargo.toml,${SHIPPED_CRATES})
	@touch $(patsubst %,${TRANSLATION_DIR}/%/Cargo.toml,${SHIPPED_CRATES}) \
      $(patsubst %,${TRANSLATION_DIR}/%-ffi/src/lib.rs,${TARGETS_LIB}) \
      $(patsubst %,${TRANSLATION_DIR}/%-rs/src/main.rs,${TARGETS_BIN})
${TRANSLATION_DIR}/%-ffi/src/lib.rs ${TRANSLATION_DIR}/%-ffi/Cargo.toml ${TRANSLATION_DIR}/%-rs/src/main.rs ${TRANSLATION_DIR}/%-rs/Cargo.toml: private RUN_ARGS += --writable "${CURDIR}/${TRANSLATION_DIR}"
ifneq ($(filter-out null,$(strip ${TRANSLATION_TEST})),)
ifneq ($(abspath ${TESTGEN_CACHE}),$(abspath ${TRANSLATION_DIR}))
${TRANSLATION_DIR}/%-ffi/src/lib.rs ${TRANSLATION_DIR}/%-ffi/Cargo.toml ${TRANSLATION_DIR}/%-rs/src/main.rs ${TRANSLATION_DIR}/%-rs/Cargo.toml: private RUN_ARGS += --readable "${CURDIR}/${TESTGEN_CACHE}"
endif
endif
${TRANSLATION_DIR}/%-ffi/src/lib.rs ${TRANSLATION_DIR}/%-ffi/Cargo.toml &: ${TRANSLATION_DIR}/%-sys/src/lib.c | ${TRANSLATION_DIR}/%-sys/Cargo.toml ${TRANSLATION_TEST_DEP}
	-${DOCKER_RUN} ${PYTHON} -m ideas.translate cargo_toml=${TRANSLATION_DIR}/$*-ffi/Cargo.toml \
                                                bindings_cargo_toml=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
                                                template=lib \
                                                vcs=${VCS} \
                                                tests=${TRANSLATION_TEST_ARG} \
                                                'deps=[openssl,flate2,regex,rustix]' \
                                                model=${PROVIDER} \
                                                $(if ${BASE_URL},model.base_url=${BASE_URL}) \
                                                model.name=${MODEL} \
                                                model.reasoning_effort=${REASONING_EFFORT} \
                                                hydra.output_subdir=.translate \
                                                hydra.job.name=translate \
                                                hydra.run.dir=${TRANSLATION_DIR}/$*-ffi ${TRANSLATE_ARGS}
	@touch ${TRANSLATION_DIR}/$*-ffi/Cargo.toml ${TRANSLATION_DIR}/$*-ffi/src/lib.rs
	@echo ""
${TRANSLATION_DIR}/%-rs/src/main.rs ${TRANSLATION_DIR}/%-rs/Cargo.toml &: ${TRANSLATION_DIR}/%-sys/src/lib.c | ${TRANSLATION_DIR}/%-sys/Cargo.toml ${TRANSLATION_TEST_DEP}
	-${DOCKER_RUN} ${PYTHON} -m ideas.translate cargo_toml=${TRANSLATION_DIR}/$*-rs/Cargo.toml \
                                                bindings_cargo_toml=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
                                                vcs=${VCS} \
                                                tests=${TRANSLATION_TEST_ARG} \
                                                'deps=[openssl,flate2,regex,rustix]' \
                                                model=${PROVIDER} \
                                                $(if ${BASE_URL},model.base_url=${BASE_URL}) \
                                                model.name=${MODEL} \
                                                model.reasoning_effort=${REASONING_EFFORT} \
                                                hydra.output_subdir=.translate \
                                                hydra.job.name=translate \
                                                hydra.run.dir=${TRANSLATION_DIR}/$*-rs ${TRANSLATE_ARGS}
	@touch ${TRANSLATION_DIR}/$*-rs/Cargo.toml ${TRANSLATION_DIR}/$*-rs/src/main.rs
	@echo ""


# cost
.PHONY: cost
cost: ${TRANSLATION_DIR}/cost.tsv
	@:  # empty rule to suppress "Nothing to be done for"
${TRANSLATION_DIR}/cost.tsv: $(patsubst %,${TRANSLATION_DIR}/%/cost.tsv,${SHIPPED_CRATES})
	@cat $^ | sort -k1 > $@
${TRANSLATION_DIR}/%/cost.tsv: ${TRANSLATION_DIR}/%/translate.log
	@cat $^ | awk 'match($$0,/^\[[^]]+\]\[([^]]+)\]\[[^]]+\].*`([^`]+)`[^$$]*([$$][0-9.]+), ([0-9,]+) tok \(([0-9,]+) in \/ ([0-9,]+) out\)/,m){printf "%s\t%s\t%s\t%s\t%s\t%s\n",m[1],m[2],m[3],m[4],m[5],m[6]}' | sort -k1,2 > $@


# build
.PHONY: build
.PRECIOUS: ${TRANSLATION_DIR}/build.log ${TRANSLATION_DIR}/%/build.log
build: ${TRANSLATION_DIR}/build.log
	@:  # empty rule to suppress "Nothing to be done for"
${TRANSLATION_DIR}/build.log: $(patsubst %,${TRANSLATION_DIR}/%/build.log,${SHIPPED_CRATES})
	@cat $^ > $@
${TRANSLATION_DIR}/%/build.log: private RUN_ARGS += --writable "${CURDIR}"
${TRANSLATION_DIR}/%/build.log: | ${TRANSLATION_DIR}/%/Cargo.toml
	-${DOCKER_RUN} env CARGO_BUILD_JOBS=1 RUSTFLAGS="${RUSTFLAGS} -Awarnings" \
        ${CARGO} build --manifest-path ${TRANSLATION_DIR}/Cargo.toml -p $* 2> ${TRANSLATION_DIR}/$*/build.log
	@cat ${TRANSLATION_DIR}/$*/build.log


# test
.PHONY: test
.PRECIOUS: ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log ${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log
.PRECIOUS: ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl ${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.jsonl
test: ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log ${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl ${TRANSLATION_DIR}/build.log
	@:  # empty rule to suppress "Nothing to be done for"
${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.log: $(patsubst %,${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log,${SHIPPED_CRATES})
	@cat $^ > $@
${TRANSLATION_DIR}/cargo_${EVALUATION_TEST}.jsonl: $(patsubst %,${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.jsonl,${SHIPPED_CRATES})
	@cat $^ > $@
${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log ${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.jsonl &: private RUN_ARGS += --writable "${CURDIR}"
${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.log ${TRANSLATION_DIR}/%/cargo_${EVALUATION_TEST}.jsonl &: ${TRANSLATION_DIR}/%/build.log ${TRANSLATION_DIR}/%/tests/${EVALUATION_TEST}.rs
ifneq ($(wildcard runner/Cargo.toml),)
	-${DOCKER_RUN} env RUSTUP_TOOLCHAIN=${RUNNER_TOOLCHAIN} CARGO_BUILD_JOBS=1 \
        ${CARGO} build --manifest-path runner/Cargo.toml --release
endif
	-${DOCKER_RUN} env NEXTEST_EXPERIMENTAL_LIBTEST_JSON=1 CARGO_BUILD_JOBS=1 \
        ${CARGO} nextest run --manifest-path ${TRANSLATION_DIR}/Cargo.toml -p $* --test ${EVALUATION_TEST} \
                             --message-format=libtest-json \
                             --no-fail-fast --no-tests=pass \
                             --build-jobs 1 --test-threads 1 \
                             --color=always \
                             --success-output=never --failure-output=never \
                             --status-level=none --final-status-level=all \
                             > ${TRANSLATION_DIR}/$*/cargo_${EVALUATION_TEST}.jsonl \
                             2> ${TRANSLATION_DIR}/$*/cargo_${EVALUATION_TEST}.log


# idiomaticity
.PHONY: idiomaticity
idiomaticity: $(patsubst %,${TRANSLATION_DIR}/%/idiomaticity.json,$(filter $(addsuffix ${IDIOMATICITY_SUFFIX},${TARGETS}),${EXISTING_CRATES}))
	@:  # empty rule to suppress "Nothing to be done for"
${TRANSLATION_DIR}/%${IDIOMATICITY_SUFFIX}/idiomaticity.json: ${TRANSLATION_DIR}/target/idiomaticity/%${IDIOMATICITY_SUFFIX}/idiomaticity.json
	@rm -rf $(@D)/.idiomaticity $(@D)/idiomaticity.log
	@cp $(<D)/idiomaticity.log $(@D)/idiomaticity.log 2>/dev/null || true  # an early failure leaves no transcript
	@cp -r $(<D)/.idiomaticity $(@D)/.idiomaticity 2>/dev/null || true
	@cp $< $@
${TRANSLATION_DIR}/target/idiomaticity/%/idiomaticity.json: private RUN_ARGS += --readable "${CURDIR}/${TRANSLATION_DIR}"
${TRANSLATION_DIR}/target/idiomaticity/%/idiomaticity.json: private RUN_ARGS += --writable "${CURDIR}/${TRANSLATION_DIR}/target"
${TRANSLATION_DIR}/target/idiomaticity/%/idiomaticity.json:
	@rm -rf $(@D)
	@mkdir -p $(@D)
	-${DOCKER_RUN} ${PYTHON} -m ideas.idiomaticity cargo_toml=${TRANSLATION_DIR}/$*/Cargo.toml \
                                                   output=$@ \
                                                   model=${PROVIDER} \
                                                   model.name=${MODEL} \
                                                   model.reasoning_effort=${REASONING_EFFORT} \
                                                   ub_toolchain=${RAPX_TOOLCHAIN} \
                                                   hydra.job.name=idiomaticity \
                                                   hydra.run.dir=$(@D)
	@[ -e $@ ] || : > $@  # empty marks a failed judgement


# convert cando tests
.PRECIOUS: ${TRANSLATION_DIR}/%-ffi/tests/test_cases.rs ${TRANSLATION_DIR}/%-rs/tests/test_cases.rs
${TRANSLATION_DIR}/%-ffi/tests/test_cases.rs ${TRANSLATION_DIR}/%-rs/tests/test_cases.rs: private RUN_ARGS += --writable "${CURDIR}"
${TRANSLATION_DIR}/%-ffi/tests/test_cases.rs: | ${TEST_FILES} ${TRANSLATION_DIR}/%-ffi/Cargo.toml runner/Cargo.toml
	${DOCKER_RUN} ${PYTHON} -m ideas.convert_tests runner_manifest=runner/Cargo.toml \
                                     vcs=${VCS} \
                                     template=${MAKEFILE_DIR}/tools/rust_tests/lib_testing.rs \
                                     output=tests/test_cases.rs \
                                     'test_vectors=[$(shell echo "$(TEST_FILES)" | tr ' ' ',')]' \
                                     hydra.output_subdir=.convert_tests \
                                     hydra.run.dir=${TRANSLATION_DIR}/$*-ffi
${TRANSLATION_DIR}/%-rs/tests/test_cases.rs: | ${TEST_FILES} ${TRANSLATION_DIR}/%-rs/Cargo.toml
	${DOCKER_RUN} ${PYTHON} -m ideas.convert_tests vcs=${VCS} \
                                     output=tests/test_cases.rs \
                                     'test_vectors=[$(shell echo "$(TEST_FILES)" | tr ' ' ',')]' \
                                     hydra.output_subdir=.convert_tests \
                                     hydra.run.dir=${TRANSLATION_DIR}/$*-rs

# generate and run I/O equivalence tests
.PHONY: testgen
.PRECIOUS: ${TRANSLATION_DIR}/testgen_io.jsonl
testgen: ${TRANSLATION_DIR}/testgen_io.jsonl ${TRANSLATION_DIR}/testgen_io.tsv
	@:  # empty rule to suppress "Nothing to be done for"
${TRANSLATION_DIR}/testgen_io.jsonl: $(patsubst %,${TRANSLATION_DIR}/%-sys/cargo_io.jsonl,${TARGETS})
	@cat $^ > $@
.PRECIOUS: ${TRANSLATION_DIR}/%-sys/tests/io.rs
${TRANSLATION_DIR}/%-sys/tests/io.rs: private RUN_ARGS += --writable "${CURDIR}/${TRANSLATION_DIR}"
${TRANSLATION_DIR}/%-sys/tests/io.rs: | ${TRANSLATION_DIR}/%-sys/Cargo.toml
	${DOCKER_RUN} ${PYTHON} -m ideas.agents.generate_io_tests model=${PROVIDER} \
                                                              $(if ${BASE_URL},model.base_url=${BASE_URL}) \
                                                              model.name=${TESTGEN_MODEL} \
                                                              model.reasoning_effort=${REASONING_EFFORT} \
                                                              manifest=${TRANSLATION_DIR}/$*-sys/Cargo.toml \
                                                              budget=${TESTGEN_BUDGET} \
                                                              steps=${TESTGEN_STEPS} \
                                                              coverage=${TESTGEN_COVERAGE} \
                                                              hydra.output_subdir=.generate_io_tests \
                                                              hydra.job.name=generate_io_tests \
                                                              hydra.run.dir=${TRANSLATION_DIR}/$*-sys
.PRECIOUS: ${TRANSLATION_DIR}/%-sys/cargo_io.jsonl ${TRANSLATION_DIR}/%-sys/cargo_io.log
${TRANSLATION_DIR}/%-sys/cargo_io.jsonl ${TRANSLATION_DIR}/%-sys/cargo_io.log &: private RUN_ARGS += --writable "${CURDIR}"
${TRANSLATION_DIR}/%-sys/cargo_io.jsonl ${TRANSLATION_DIR}/%-sys/cargo_io.log &: ${TRANSLATION_DIR}/%-sys/Cargo.toml ${TRANSLATION_DIR}/%-sys/tests/io.rs
	-${DOCKER_RUN} env NEXTEST_EXPERIMENTAL_LIBTEST_JSON=1 CARGO_BUILD_JOBS=1 \
        ${CARGO} nextest run --manifest-path $< --test io \
                             --message-format=libtest-json \
                             --no-fail-fast --no-tests=pass \
                             --build-jobs 1 --test-threads 1 \
                             --color=always \
                             --success-output=never --failure-output=never \
                             --status-level=none --final-status-level=all \
                             > ${TRANSLATION_DIR}/$*-sys/cargo_io.jsonl \
                             2> ${TRANSLATION_DIR}/$*-sys/cargo_io.log

# measure branch coverage of the generated I/O tests
.PRECIOUS: ${TRANSLATION_DIR}/testgen_io.tsv ${TRANSLATION_DIR}/%-sys/coverage_io.log
${TRANSLATION_DIR}/testgen_io.tsv: $(patsubst %,${TRANSLATION_DIR}/%-sys/coverage_io.log,${TARGETS})
	@for log in $^; do \
      cov=$$(sed -n 's/^Branch coverage: \([0-9.]*\)%.*/\1/p' "$$log" | tail -n1); \
      printf '%s\t%s\n' "$$(basename $$(dirname $$log))" "$${cov:-N/A}"; \
    done > $@
${TRANSLATION_DIR}/%-sys/coverage_io.log: private RUN_ARGS += --writable "${CURDIR}"
${TRANSLATION_DIR}/%-sys/coverage_io.log: ${TRANSLATION_DIR}/%-sys/cargo_io.jsonl
	-${DOCKER_RUN} env -C $(@D) bash instrument.sh io > $@ 2>&1

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
clean: private RUN_ARGS += --writable "${CURDIR}"
clean:
	rm -rf build-ninja
ifneq ($(wildcard ${TRANSLATION_DIR}/Cargo.toml),)
	${DOCKER_RUN} ${CARGO} clean --manifest-path ${TRANSLATION_DIR}/Cargo.toml
endif
