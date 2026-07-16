#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))

AGENT_PROVIDER ?= openrouter
AGENT_MODEL ?= anthropic/claude-sonnet-4.6
AGENT_BASE_URL ?= "https://openrouter.ai/api/v1"

# Docker configuration
DOCKER_HOSTDIR := ${MAKEFILE_DIR}/docker
DOCKER_WORKDIR := /home/user/IDEAS
DOCKER_RUN ?= docker run --rm \
    --init \
    -it \
    -v $(MAKEFILE_DIR):$(DOCKER_WORKDIR) \
    -w $(DOCKER_WORKDIR)/$(patsubst $(MAKEFILE_DIR)/%,%,$(CURDIR)) \
    -e OPENROUTER_API_KEY \
    -e TRANSLATION_DIR \
    -e AGENT_PROVIDER \
    -e AGENT_MODEL \
    -e AGENT_BASE_URL \
    -e RUSTFLAGS \
    ideas-$(shell id -u)

ifdef DOCKER_RUN
	RUN_PREFIX = \
        mkdir -p $(DOCKER_HOSTDIR)/.venv && \
        $(DOCKER_RUN) \
        /bin/sh -c 'set -e;
	RUN_SUFFIX = '
else
	RUN_PREFIX =
	RUN_SUFFIX =
endif

TARGETS_LIB ?= $(shell [ -d build-ninja ] && find build-ninja -maxdepth 1 -type f -executable -exec basename {} \; | cut -d. -f1 | grep -E "^lib" | sed -e "s/^lib//gi")
TARGETS_BIN ?= $(shell [ -d build-ninja ] && find build-ninja -maxdepth 1 -type f -executable -exec basename {} \; | cut -d. -f1 | grep -vE "^lib")
TARGETS ?= $(TARGETS_BIN) $(TARGETS_LIB)
ifeq (${TARGETS},)
ifeq ($(filter cmake clean,$(MAKECMDGOALS)),)
$(error No TARGETS found! You need to run cmake!)
endif
endif


.PRECIOUS: test_crates/%/Cargo.toml
.PRECIOUS: test_crates/%/src/lib.c
.PRECIOUS: test_crates/%/src/main.c
.PRECIOUS: test_crates/%/build.rs

test_crates/%/Cargo.toml \
test_crates/%/src/lib.c \
test_crates/%/build.rs: | build-ninja/lib%.so.sources
	uv run python -m ideas.init.crate cargo_toml=test_crates/$*/Cargo.toml \
                                  template=lib \
                                  reexport_lib=false \
                                  hydra.output_subdir=null \
                                  hydra.run.dir=test_crates/$*
	uv run python -m ideas.init.consolidate compile_commands=build-ninja/compile_commands.json \
                                        cargo_toml=test_crates/$*/Cargo.toml \
                                        source_priority=build-ninja/lib$*.so.sources \
                                        hydra.output_subdir=null \
                                        hydra.run.dir=test_crates/$*
	uv run python -m ideas.agents.build instrumentation=coverage \
                                    hydra.output_subdir=null \
                                    hydra.job.name=init.build \
                                    hydra.run.dir=test_crates/$*

test_crates/%/Cargo.toml \
test_crates/%/src/main.c \
test_crates/%/build.rs: | build-ninja/%.sources
	uv run python -m ideas.init.crate cargo_toml=test_crates/$*/Cargo.toml \
                                  template=bin \
                                  hydra.output_subdir=null \
                                  hydra.run.dir=test_crates/$*
	uv run python -m ideas.init.consolidate compile_commands=build-ninja/compile_commands.json \
                                        cargo_toml=test_crates/$*/Cargo.toml \
                                        source_priority=build-ninja/$*.sources \
                                        hydra.output_subdir=null \
                                        hydra.run.dir=test_crates/$*
	uv run python -m ideas.agents.build instrumentation=coverage \
                                    hydra.output_subdir=null \
                                    hydra.job.name=init.build \
                                    hydra.run.dir=test_crates/$*


.PHONY: testgen_agent
testgen_agent: $(patsubst %,test_crates/%/tests/test_assert.rs,${TARGETS}) ;

.PRECIOUS: test_crates/%/tests/test_assert.rs
test_crates/%/tests/test_assert.rs: test_crates/%/Cargo.toml test_crates/%/src/lib.c | build-ninja/lib%.so.sources
	$(RUN_PREFIX) \
        uv run python -m ideas.agents.testgen model=$(if $(AGENT_PROVIDER),${AGENT_PROVIDER}/,)${AGENT_MODEL} \
            cargo_toml=test_crates/$*/Cargo.toml \
            c_code=test_crates/$*/src/lib.c \
            project_name=$* \
            test_crate_out=test_crates/$* \
            hydra.output_subdir=null \
            hydra.job.name=testgen \
            hydra.run.dir=test_crates/$* \
    $(RUN_SUFFIX)
	$(RUN_PREFIX) \
        uv run python -m ideas.agents.testgen model=$(if $(AGENT_PROVIDER),${AGENT_PROVIDER}/,)${AGENT_MODEL} \
            guarantee_assert_tests=true \
            collect_to_assert=true \
            cargo_toml=test_crates/$*/Cargo.toml \
            c_code=test_crates/$*/src/lib.c \
            project_name=$* \
            test_crate_out=test_crates/$* \
            hydra.output_subdir=null \
            hydra.job.name=assert_writer \
            hydra.run.dir=test_crates/$* \
    $(RUN_SUFFIX)
    # Agents are not guaranteed to produce the file
	[ -f test_crates/$*/tests/test_assert.rs ] || { echo "ERROR: Agent failed to generate test_crates/$*/tests/test_assert.rs"; exit 1; }

test_crates/%/tests/test_assert.rs: test_crates/%/Cargo.toml test_crates/%/src/main.c | build-ninja/%.sources
	$(RUN_PREFIX) \
        uv run python -m ideas.agents.testgen_bin model=$(if $(AGENT_PROVIDER),${AGENT_PROVIDER}/,)${AGENT_MODEL} \
            cargo_toml=test_crates/$*/Cargo.toml \
            c_code=test_crates/$*/src/main.c \
            project_name=$* \
            test_crate_out=test_crates/$* \
            hydra.output_subdir=null \
            hydra.job.name=testgen \
            hydra.run.dir=test_crates/$* \
    $(RUN_SUFFIX)
	$(RUN_PREFIX) \
        uv run python -m ideas.agents.testgen_bin model=$(if $(AGENT_PROVIDER),${AGENT_PROVIDER}/,)${AGENT_MODEL} \
            guarantee_assert_tests=true \
            collect_to_assert=true \
            cargo_toml=test_crates/$*/Cargo.toml \
            c_code=test_crates/$*/src/main.c \
            project_name=$* \
            test_crate_out=test_crates/$* \
            hydra.output_subdir=null \
            hydra.job.name=assert_writer \
            hydra.run.dir=test_crates/$* \
    $(RUN_SUFFIX)
    # Agents are not guaranteed to produce the file
	[ -f test_crates/$*/tests/test_assert.rs ] || { echo "ERROR: Agent failed to generate test_crates/$*/tests/test_assert.rs"; exit 1; }
