MAKEFILE_DIR := $(realpath $(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
IDEAS_MAKEFILE := $(MAKEFILE_DIR)/IDEAS.mk

PROVIDER = openai## Provider to use with DSPy/LiteLLM
MODEL = gpt-5.6-sol## Model to use to translate
HOST = localhost
PORT = 8000## Port to use for vLLM
BASE_URL = http://${HOST}:${PORT}/v1## Base URL of vLLM server
GIT_HEAD := $(shell git --git-dir=${MAKEFILE_DIR}/.git rev-parse HEAD 2>/dev/null || echo "ideas")
TRANSLATION_DIR ?= translation.${GIT_HEAD}## Directory to put IDEAS translation
CARGO_NET_OFFLINE = true## Cargo offline mode
RUSTFLAGS = -Awarnings## Flags to build Rust translation
CC = clang
CFLAGS = -w## Ignore C compiler warnings
TRANSLATION_TEST = null## Translation test directory/name to run (empty/null: wrap globals only, no tests)
EVALUATION_TEST = test_cases## Evaluation test directory/name to run
VCS = git## Whether to use version control during translation. Options: ['git', 'none']
GIT_AUTHOR_NAME = ideas.${MODEL}
GIT_AUTHOR_EMAIL = ${MODEL}@${PROVIDER}
TRANSLATE_ARGS = ## Args to pass to IDEAS translation
TESTGEN_BUDGET = 4.0## Budget [USD] for IDEAS test generation
TESTGEN_COVERAGE = 100## Requested branch coverage [%] for IDEAS test generation
VERBOSE = 0## Whether to output failed/partial projects in summaries

DOCKER_RUN = mkdir -p ${MAKEFILE_DIR}/docker/venv && docker run --rm \
    --init \
    --mount type=bind,src=${MAKEFILE_DIR},dst=${MAKEFILE_DIR},readonly \
    --mount type=bind,src=${MAKEFILE_DIR}/docker/venv,dst=${MAKEFILE_DIR}/.venv \
    -w ${CURDIR} \
    -e OPENROUTER_API_KEY -e OPENAI_API_KEY \
    -e RUSTFLAGS \
    -e GIT_AUTHOR_NAME -e GIT_AUTHOR_EMAIL
USER_UID := $(shell id -u)
DOCKER_IMAGE = $(if ${DOCKER_RUN},ideas-${USER_UID},)

ifeq (${PROVIDER},hosted_vllm)
override TRANSLATE_ARGS += model.base_url=${BASE_URL}
override TRANSLATE_ARGS += generate.timeout=5400
endif

# Directories reserved for IDEAS
TRANSLATION_DIR_RESERVED := test_case build-ninja test_vectors runner
ifeq ($(strip ${TRANSLATION_DIR}),)
$(error TRANSLATION_DIR must not be empty)
endif
ifneq ($(filter ${TRANSLATION_DIR},${TRANSLATION_DIR_RESERVED}),)
$(error TRANSLATION_DIR='${TRANSLATION_DIR}' is reserved (${TRANSLATION_DIR_RESERVED}); pick another name)
endif

TARGETS_LIB := $(shell [ -d build-ninja ] && find build-ninja -maxdepth 1 -type d -name '*.so.d' -printf '%f\n' | sed 's/\.so\.d$$//')
TARGETS_BIN := $(shell [ -d build-ninja ] && find build-ninja -maxdepth 1 -type d -name '*.d' ! -name '*.so.d' -printf '%f\n' | sed 's/\.d$$//')
TARGETS = ${TARGETS_LIB} ${TARGETS_BIN}

export
