#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

RUN_ARGS =
ENV_ARGS =
# Keep wrapper command traces visible when tool stderr is redirected to a log
DOCKER_RUN = @3>&2 ${MAKEFILE_DIR}/docker/run.sh ${ENV_ARGS} ${RUN_ARGS} --

ifdef OPENAI_BASE_URL
ENV_ARGS += OPENAI_BASE_URL="${OPENAI_BASE_URL}"
endif
ifdef MAX_DEPENDENT_CHARS
ENV_ARGS += MAX_DEPENDENT_CHARS="${MAX_DEPENDENT_CHARS}"
endif
ifdef REDUCED_CONTEXT
ENV_ARGS += REDUCED_CONTEXT="${REDUCED_CONTEXT}"
endif
ifdef TESTGEN_BOTTOM_UP
ENV_ARGS += TESTGEN_BOTTOM_UP="${TESTGEN_BOTTOM_UP}"
endif
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
ENV_ARGS += RUSTFLAGS="${RUSTFLAGS}"

IDEAS_DOCKER_IMAGE ?= ideas-$(shell id -u)## Docker image used for translation tooling; empty for native execution
IDEAS_DOCKER_IMAGE := $(IDEAS_DOCKER_IMAGE)
DOCKER_READY :=

ifneq ($(strip ${IDEAS_DOCKER_IMAGE}),)
DOCKER_READY := docker/venv/.ready

RUN_ARGS += --image "${IDEAS_DOCKER_IMAGE}"
RUN_ARGS += --ideas-dir "${MAKEFILE_DIR}"
RUN_ARGS += --workdir "${CURDIR}"
ifeq (${DOCKER_INSPECT},1)
RUN_ARGS += --inspect
endif # DOCKER_INSPECT

ifeq (${INCLUDE_DOCKER_RULES},1)
.PHONY: docker
docker: private RUN_ARGS += --tty
docker:## Open an interactive Docker shell
docker: ${DOCKER_READY}
	${DOCKER_RUN} bash

.PRECIOUS: docker/build.log
docker/build.log: docker/ideas.Dockerfile uv.lock pyproject.toml INSTALL.mk
	docker build --build-arg USER_UID=$(shell id -u) \
                 --build-arg USER_GID=$(shell id -g) \
                 -f docker/ideas.Dockerfile -t ${IDEAS_DOCKER_IMAGE} .
	docker images --quiet ${IDEAS_DOCKER_IMAGE}:latest > $@

.PHONY: docker/build
docker/build:## Build the Docker image
docker/build: docker/build.log

docker/venv/.ready: docker/build.log uv.lock pyproject.toml
	${DOCKER_RUN} uv sync --frozen
	@touch $@

.PHONY: docker-clean
docker-clean:
	rm -f docker/build.log
clean: docker-clean

.PHONY: examples/docker
examples/docker:## Open an interactive Docker shell with all EXAMPLES ready to translate
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
examples/%/docker:## Open an interactive Docker shell for a specific example ready to translate
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
endif # INCLUDE_DOCKER_RULES
endif # IDEAS_DOCKER_IMAGE
