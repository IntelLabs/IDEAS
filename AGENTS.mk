#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))
EXTRACT_INFO_CMAKE := ${MAKEFILE_DIR}/extract_info.cmake
IDEAS_MAKEFILE := $(MAKEFILE_DIR)/IDEAS.mk

ANTHROPIC_AUTH_TOKEN ?= $(OPENROUTER_API_KEY)
ANTHROPIC_BASE_URL ?= https://openrouter.ai/api
ANTHROPIC_API_KEY ?= ""

AGENT_PROVIDER ?= openrouter
AGENT_MODEL ?= anthropic/claude-sonnet-4.6
BASE_URL ?= "https://openrouter.ai/api/v1"
TRANSLATION_DIR ?= translation.$(shell git --git-dir=${MAKEFILE_DIR}/.git rev-parse HEAD)

export EXTRACT_INFO_CMAKE

TARGETS ?= $(shell [ -d build-ninja ] && find build-ninja -maxdepth 1 -type f -executable -exec basename {} \; | cut -d. -f1 | sed -e "s/^lib//gi")
ifeq (${TARGETS},)
ifeq ($(filter cmake clean,$(MAKECMDGOALS)),)
$(error No TARGETS found! You need to run cmake!)
endif
endif

# Docker configuration
DOCKER_DIR := ${MAKEFILE_DIR}/docker
DOCKER_WORKDIR := /home/user/IDEAS
# Relative path to the current working directory
DOCKER_REL_CWD := $(patsubst $(MAKEFILE_DIR)/%,%,$(CURDIR))
DOCKER_RUN ?= docker run --rm \
    --init \
    -it \
    -v $(MAKEFILE_DIR):$(DOCKER_WORKDIR) \
    -v $(DOCKER_DIR)/.venv:$(DOCKER_WORKDIR)/.venv \
    -e OPENROUTER_API_KEY \
    -e TRANSLATION_DIR \
    -e AGENT_PROVIDER \
    -e AGENT_MODEL \
    -e BASE_URL \
    -e RUSTFLAGS \
    -e VERBOSE \
    ideas-$(shell id -u)

ifdef DOCKER_RUN
    # Touch directory for correct permissions when mounted
	VENV_SETUP = mkdir -p $(DOCKER_DIR)/.venv
    # Run inside Docker container with exit-on-error
	RUN_CMD = $(DOCKER_RUN) /bin/sh -c 'set -e; cd $(DOCKER_WORKDIR)/$(DOCKER_REL_CWD); $(1)'
else
	VENV_SETUP = @true
	RUN_CMD = $(1)
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

# test generation from project
.PHONY: testgen
testgen: test_crate/tests/test_assert.rs ;

.PRECIOUS: test_crate/tests/test_assert.rs
test_crate/tests/test_assert.rs:
	$(VENV_SETUP)
	$(call RUN_CMD,\
        uv run python -m ideas.agents.testgen model=$(if $(AGENT_PROVIDER),${AGENT_PROVIDER}/,)${AGENT_MODEL} \
            c_code=test_case \
            project_name=$(notdir $(CURDIR)) \
            test_vectors_out=test_vectors/agent \
            test_crate_out=test_crate \
            hydra.output_subdir=.testgen \
            hydra.job.name=testgen \
            hydra.run.dir=test_vectors; \
    )


# library targets: generate tests from the consolidated lib.c
.PRECIOUS: test_crates/%/tests/test_assert.rs
test_crates/%/tests/test_assert.rs: ${TRANSLATION_DIR}/%/src/lib.c | build-ninja/lib%.so.type
    # Copy lib.c into test_targets/<target>/src/ so
    # build.rs can use ../../test_targets/<target>/src/lib.c
    # both in Docker /tmp and on disk
	mkdir -p test_targets/$*/src
	cp ${TRANSLATION_DIR}/$*/src/lib.c test_targets/$*/src/lib.c
	$(VENV_SETUP)
	$(call RUN_CMD,\
        uv run python -m ideas.agents.testgen model=$(if $(AGENT_PROVIDER),${AGENT_PROVIDER}/,)${AGENT_MODEL} \
            c_code=test_targets/$*/src/lib.c \
            project_name=$* \
            test_vectors_out=test_vectors/$*/agent \
            test_crate_out=test_crates/$* \
            hydra.output_subdir=.testgen \
            hydra.job.name=testgen \
            hydra.run.dir=test_vectors/$*; \
    )

# executable targets: do nothing
test_crates/%/tests/test_assert.rs: ${TRANSLATION_DIR}/%/src/main.c | build-ninja/%.type
	mkdir -p test_crates/$*/tests
	touch test_crates/$*/tests/test_assert.rs

# fallback
test_crates/%/tests/test_assert.rs:
	mkdir -p test_crates/$*/tests
	touch test_crates/$*/tests/test_assert.rs
