#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

RUN_ARGS =
ENV_ARGS =
# Keep wrapper command traces visible when tool stderr is redirected to a log.
# Drop make's jobserver from the step's environment: its fds don't reach the step (fd 3 is
# reused for traces), and cargo would warn about it into the logs. UV_NO_SYNC makes uv warn
# about the --script wrapper, so it's only forwarded to the step (see below).
DOCKER_RUN = @3>&2 env -u MAKEFLAGS -u MFLAGS -u UV_NO_SYNC uv run --script ${MAKEFILE_DIR}/src/ideas/sandbox.py ${ENV_ARGS} ${RUN_ARGS} --

ifdef TESTGEN_FINISH_EARLY
ENV_ARGS += TESTGEN_FINISH_EARLY="${TESTGEN_FINISH_EARLY}"
endif

ENV_ARGS += CC="${CC}"
ENV_ARGS += CFLAGS="${CFLAGS}"
ENV_ARGS += CARGO_NET_OFFLINE="${CARGO_NET_OFFLINE}"
ENV_ARGS += DEBUGINFOD_URLS="${DEBUGINFOD_URLS}"
ENV_ARGS += GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME}"
ENV_ARGS += GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL}"
ENV_ARGS += GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME}"
ENV_ARGS += GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL}"
ifdef RUSTFLAGS
ENV_ARGS += RUSTFLAGS="${RUSTFLAGS}"
endif
ENV_ARGS += RUSTUP_TOOLCHAIN="${RUSTUP_TOOLCHAIN}"
ENV_ARGS += RUNNER_TOOLCHAIN="${RUNNER_TOOLCHAIN}"

IDEAS_DOCKER_IMAGE ?= ideas-$(shell id -u)## Docker image for the default IDEAS_RUNNER
IDEAS_DOCKER_IMAGE := $(IDEAS_DOCKER_IMAGE)
IDEAS_RUNNER ?= docker:${IDEAS_DOCKER_IMAGE}## Where steps run: docker:<image>, landlock (sandboxed without Docker), or native (unconfined)
RUNNER := $(strip ${IDEAS_RUNNER})
ifeq (${RUNNER},docker:)
$(error IDEAS_RUNNER=docker: needs an image; set IDEAS_DOCKER_IMAGE, or pick IDEAS_RUNNER=landlock or native)
endif
ifneq ($(words ${RUNNER})$(filter native landlock docker:%,${RUNNER}),1${RUNNER})
$(error IDEAS_RUNNER must be docker:<image>, landlock or native, not '${IDEAS_RUNNER}')
endif
DOCKER_IMAGE := $(patsubst docker:%,%,$(filter docker:%,${RUNNER}))
DOCKER_READY :=

RUN_ARGS += --runner "${RUNNER}"
# Docker and Landlock set UV_NO_SYNC=1 themselves; native steps inherit it from the caller
ifeq (${RUNNER},native)
ifdef UV_NO_SYNC
ENV_ARGS += UV_NO_SYNC="${UV_NO_SYNC}"
endif
endif
RUN_ARGS += --root "${MAKEFILE_DIR}"
RUN_ARGS += --hide examples
RUN_ARGS += --workdir "${CURDIR}"

ifneq (${DOCKER_IMAGE},)
DOCKER_READY := docker/build

ifeq (${DOCKER_INSPECT},1)
RUN_ARGS += --inspect
endif # DOCKER_INSPECT

ifeq (${INCLUDE_DOCKER_RULES},1)
.PHONY: docker/build
docker/build:## Build the Docker image
docker/build: docker/venv/.ready docker/cargo/.ready

.PRECIOUS: docker/build.log
docker/build.log: docker/ideas.Dockerfile uv.lock pyproject.toml INSTALL.mk
	docker build --build-arg USER_UID=$(shell id -u) \
             --build-arg USER_GID=$(shell id -g) \
             -f docker/ideas.Dockerfile -t ${DOCKER_IMAGE} .
	docker images --quiet ${DOCKER_IMAGE}:latest > $@

.PRECIOUS: docker/venv/.ready
docker/venv/.ready: docker/build.log uv.lock pyproject.toml
	${DOCKER_RUN} uv sync --frozen
	@touch $@

.PRECIOUS: docker/cargo/.ready
docker/cargo/.ready: private RUN_ARGS += --writable ${MAKEFILE_DIR}/docker/templates
docker/cargo/.ready: docker/build.log
	${DOCKER_RUN} env CARGO_NET_OFFLINE=false \
        cargo fetch --manifest-path docker/templates/workspace/Cargo.toml --locked
	${DOCKER_RUN} env CARGO_NET_OFFLINE=false RUSTUP_TOOLCHAIN=${RUNNER_TOOLCHAIN} \
        cargo fetch --manifest-path docker/templates/runner/Cargo.toml --locked
	@touch $@

.PHONY: docker-clean
docker-clean:
	rm -f docker/build.log
clean: docker-clean

endif # INCLUDE_DOCKER_RULES
endif # DOCKER_IMAGE

# Interactive shells confined like a translation step, in Docker or under Landlock
ifeq (${INCLUDE_DOCKER_RULES},1)
ifneq (${RUNNER},native)
.PHONY: docker
docker: private RUN_ARGS += --tty
docker:## Open an interactive sandboxed shell (Docker or Landlock, per IDEAS_RUNNER)
docker: ${DOCKER_READY}
	${DOCKER_RUN} bash

.PHONY: examples/docker
examples/docker:## Open an interactive sandboxed shell with all EXAMPLES ready to translate
examples/docker: private RUN_ARGS += --tty
examples/docker: private ENV_ARGS += TRANSLATION_DIR="${TRANSLATION_DIR}"
examples/docker: private RUN_ARGS += $(foreach example,${EXAMPLES},--readable "${MAKEFILE_DIR}/${example}/test_case")
examples/docker: private RUN_ARGS += $(foreach example,${EXAMPLES},$(if $(wildcard ${MAKEFILE_DIR}/${example}/CMakePresets.json),--readable "${MAKEFILE_DIR}/${example}/CMakePresets.json"))
examples/docker: private RUN_ARGS += $(foreach example,${EXAMPLES},$(if $(wildcard ${MAKEFILE_DIR}/${example}/CMakeLists.txt),--readable "${MAKEFILE_DIR}/${example}/CMakeLists.txt"))
examples/docker: private RUN_ARGS += $(foreach example,${EXAMPLES},--writable "${MAKEFILE_DIR}/${example}/build-ninja")
examples/docker: private RUN_ARGS += $(foreach example,${EXAMPLES},--writable "${MAKEFILE_DIR}/${example}/${TRANSLATION_DIR}")
examples/docker: ${DOCKER_READY} FORCE
	@mkdir -p $(foreach example,${EXAMPLES},"${example}/build-ninja" "${example}/${TRANSLATION_DIR}")
	${DOCKER_RUN} bash
examples/%/docker:## Open an interactive sandboxed shell for a specific example ready to translate
examples/%/docker: private RUN_ARGS += --tty
examples/%/docker: private ENV_ARGS += TRANSLATION_DIR="${TRANSLATION_DIR}"
examples/%/docker: private RUN_ARGS += --readable "${MAKEFILE_DIR}/$(@D)/test_case"
examples/%/docker: private RUN_ARGS += $(if $(wildcard ${MAKEFILE_DIR}/$(@D)/CMakePresets.json),--readable "${MAKEFILE_DIR}/$(@D)/CMakePresets.json")
examples/%/docker: private RUN_ARGS += $(if $(wildcard ${MAKEFILE_DIR}/$(@D)/CMakeLists.txt),--readable "${MAKEFILE_DIR}/$(@D)/CMakeLists.txt")
examples/%/docker: private RUN_ARGS += --writable "${MAKEFILE_DIR}/$(@D)/build-ninja"
examples/%/docker: private RUN_ARGS += --writable "${MAKEFILE_DIR}/$(@D)/${TRANSLATION_DIR}"
examples/%/docker: ${DOCKER_READY} FORCE
	@mkdir -p "$(@D)/build-ninja" "$(@D)/${TRANSLATION_DIR}"
	${DOCKER_RUN} bash
endif # RUNNER
endif # INCLUDE_DOCKER_RULES
