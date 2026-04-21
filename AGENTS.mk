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


# test generation from project
.PHONY: testgen
testgen: test_crate/tests/test_assert.rs ;

.PRECIOUS: test_crate/tests/test_assert.rs
test_crate/tests/test_assert.rs:
	$(RUN_PREFIX) \
        uv run python -m ideas.agents.testgen model=$(if $(AGENT_PROVIDER),${AGENT_PROVIDER}/,)${AGENT_MODEL} \
            c_code=test_case \
            project_name=$(notdir $(CURDIR)) \
            test_vectors_out=test_vectors/agent \
            test_crate_out=test_crate \
            hydra.output_subdir=.testgen \
            hydra.job.name=testgen \
            hydra.run.dir=test_vectors \
    $(RUN_SUFFIX)
    # Agent is not guaranteed to write file
	[ -f test_crate/tests/test_assert.rs ] || { echo "ERROR: Agent failed to generate test_crate/tests/test_assert.rs"; exit 1; }


# library targets: generate tests from the consolidated lib.c
.PRECIOUS: test_crates/%/tests/test_assert.rs
test_crates/%/tests/test_assert.rs: ${TRANSLATION_DIR}/%/src/lib.c | build-ninja/lib%.so.type
    # Copy lib.c into test_crates/<target>/src/ so
    # build.rs can use ../../test_crates/<target>/src/lib.c
    # both in Docker /tmp and on disk
	mkdir -p test_crates/$*/src
	cp ${TRANSLATION_DIR}/$*/src/lib.c test_crates/$*/src/lib.c
	$(RUN_PREFIX) \
        uv run python -m ideas.agents.testgen model=$(if $(AGENT_PROVIDER),${AGENT_PROVIDER}/,)${AGENT_MODEL} \
            c_code=test_crates/$*/src/lib.c \
            project_name=$* \
            test_vectors_out=test_vectors/$*/agent \
            test_crate_out=test_crates/$* \
            hydra.output_subdir=.testgen \
            hydra.job.name=testgen \
            hydra.run.dir=test_vectors/$* \
    $(RUN_SUFFIX)
    # Agent is not guaranteed to write file
	[ -f test_crates/$*/tests/test_assert.rs ] || { echo "ERROR: Agent failed to generate test_crates/$*/tests/test_assert.rs"; exit 1; }

# executable targets: do nothing
test_crates/%/tests/test_assert.rs: ${TRANSLATION_DIR}/%/src/main.c | build-ninja/%.type
	$(error Agent cannot generate tests for binary targets yet!)
