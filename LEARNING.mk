#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

MAKEFILE_PATH := $(abspath $(lastword $(MAKEFILE_LIST)))
MAKEFILE_DIR := $(realpath $(dir $(MAKEFILE_PATH)))
EXAMPLES_DIR := examples/Test-Corpus/Public-Tests

PROVIDER ?= hosted_vllm## Provider to use with DSPy/LiteLLM
MODEL ?= Qwen/Qwen3-Coder-30B-A3B-Instruct## Model to use to translate
REVISION ?= None## Revision of model to load in vLLM
HOST ?= localhost
PORT ?= 8000## Port to use for vLLM
BASE_URL ?= http://${HOST}:${PORT}/v1## Base URL of vLLM server

DATA_DIR ?= translation.$(shell git rev-parse HEAD)## Directory to collect data from
TEACHER_PROVIDER ?= openrouter## GEPA teacher model provider
TEACHER_MODEL ?= openai/gpt-5-mini## GEPA teacher model name
TEACHER_BASE_URL ?= https://openrouter.ai/api/v1## GEPA teacher model base URL
REFLECT_PROVIDER ?= openrouter## GEPA reflection model provider
REFLECT_MODEL ?= openai/gpt-5.1## GEPA reflection model name
REFLECT_BASE_URL ?= https://openrouter.ai/api/v1## GEPA reflection model base URL


EXAMPLES ?= $(sort $(patsubst %/test_case,%,$(shell find ${EXAMPLES_DIR} -maxdepth 3 -name test_case -type d)))## List of examples to run on
ifeq ($(EXAMPLES),)
$(warning No projects found in ${EXAMPLES_DIR}. You may need to re-run commands!)
endif

.PRECIOUS: student_examples.lst
student_examples.lst:
	-@$(MAKE) -j128 -f ${MAKEFILE_DIR}/Makefile examples/wrapper \
      TRANSLATION_DIR=${DATA_DIR}.student \
      PROVIDER=${PROVIDER} \
      MODEL=${MODEL} \
      BASE_URL=${BASE_URL}
	@echo "$(EXAMPLES)" | tr ' ' '\n' | sort | xargs realpath | sed "s|$$|/${DATA_DIR}.student|" > $@

.PRECIOUS: teacher_examples.lst
teacher_examples.lst:
	-@$(MAKE) -j128 -f ${MAKEFILE_DIR}/Makefile examples/wrapper \
      TRANSLATION_DIR=${DATA_DIR}.teacher \
      PROVIDER=${TEACHER_PROVIDER} \
      MODEL=${TEACHER_MODEL} \
      BASE_URL=${TEACHER_BASE_URL}
	@echo "$(EXAMPLES)" | tr ' ' '\n' | sort | xargs realpath | sed "s|$$|/${DATA_DIR}.teacher|" > $@

.PHONY: learn/translate
learn/translate:## Learn a prompt for translating C to Rust
learn/translate: student_examples.lst teacher_examples.lst
	uv run python -m ideas.learn.translate \
      student_examples=$(realpath student_examples.lst) \
      teacher_examples=$(realpath teacher_examples.lst) \
      model.name=${PROVIDER}/${MODEL} \
      model.base_url=${BASE_URL} \
      reflect_model.name=${REFLECT_PROVIDER}/${REFLECT_MODEL} \
      reflect_model.base_url=${REFLECT_BASE_URL}


clean:
	rm -f student_examples.lst teacher_examples.lst
