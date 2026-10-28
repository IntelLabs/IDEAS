#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

PROVIDER = openrouter## Provider for MODEL: openrouter, openai, vllm, or azure
MODEL = openai/gpt-6-luna## Model to use to translate and generate wrappers
REASONING_EFFORT = medium## Reasoning effort to translate and generate wrappers
VLLM_PORT = 8000## Port to use for vLLM
BASE_URL = ## API endpoint override (required for vllm and azure)
GIT_HEAD := $(shell git --git-dir=${MAKEFILE_DIR}/.git rev-parse HEAD 2>/dev/null || echo "ideas")
TRANSLATION_DIR ?= translation.${GIT_HEAD}## Directory to put IDEAS translation
TRANSLATION_TIME := $(shell date -u +%Y%m%dT%H%M%SZ)
TRANSLATION_BRANCH ?= ${GIT_HEAD}.${TRANSLATION_TIME}## Branch name for translation
CARGO_NET_OFFLINE = true## Cargo offline mode
DEBUGINFOD_URLS = ## Debuginfod servers
RUSTUP_TOOLCHAIN = ${TRANSLATION_TOOLCHAIN}
NEXTEST_EXPERIMENTAL_LIBTEST_JSON ?= 1
CC = clang
CFLAGS = -w## Ignore C compiler warnings
TRANSLATION_TEST = null## Translation test directory/name to run (empty/null: wrap globals only, no tests)
EVALUATION_TEST = test_cases## Evaluation test directory/name to run
IDIOMATICITY_SUFFIX = -rs## Judge workspace member <target><suffix> (empty: <target> itself)
VCS = git## Whether to use version control during translation. Options: ['git', 'none']
GIT_AUTHOR_NAME = ${MODEL}/${REASONING_EFFORT}
GIT_AUTHOR_EMAIL = ideas@${PROVIDER}
GIT_COMMITTER_NAME = ${GIT_AUTHOR_NAME}
GIT_COMMITTER_EMAIL = ${GIT_AUTHOR_EMAIL}
TRANSLATE_ARGS = ## Args to pass to IDEAS translation
TESTGEN_CACHE ?= test_crates## Where to fetch tests from
TESTGEN_MODEL = ${MODEL}## Model name for IDEAS test generation
TESTGEN_BUDGET = 4.0## Budget [USD] for IDEAS test generation
TESTGEN_STEPS = 100## Number of steps for IDEAS test generation
TESTGEN_COVERAGE = 100## Requested branch coverage [%] for IDEAS test generation
VERBOSE = 0## Whether to output failed/partial projects in summaries

# Directories reserved for IDEAS
TRANSLATION_DIR_RESERVED := test_case build-ninja test_vectors runner translated_rust
ifeq ($(strip ${TRANSLATION_DIR}),)
$(error TRANSLATION_DIR must not be empty)
endif
ifneq ($(filter ${TRANSLATION_DIR},${TRANSLATION_DIR_RESERVED}),)
$(error TRANSLATION_DIR='${TRANSLATION_DIR}' is reserved (${TRANSLATION_DIR_RESERVED}); pick another name)
endif

# Allow users to specify which targets to build and translate by filtering the available library and binary targets
TARGETS_LIB := $(shell [ -d build-ninja ] && find build-ninja -maxdepth 1 -type d -name '*.so.d' -printf '%f\n' | sed 's/\.so\.d$$//')
TARGETS_BIN := $(shell [ -d build-ninja ] && find build-ninja -maxdepth 1 -type d -name '*.d' ! -name '*.so.d' -printf '%f\n' | sed 's/\.d$$//')
TARGETS = ${TARGETS_LIB} ${TARGETS_BIN}
TARGETS_LIB := $(filter ${TARGETS},${TARGETS_LIB})
TARGETS_BIN := $(filter ${TARGETS},${TARGETS_BIN})

export
