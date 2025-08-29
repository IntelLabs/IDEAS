#!/bin/bash

declare -A results
declare -A descriptions
declare -A translate_params

# Define experiments with descriptions
experiments=(
    "clang"
    "clang-sys-filter"
)
need_restart=false

descriptions["clang"]="clang"
descriptions["clang-sys-filter"]="clang with system libraries filtered"

# Translation parameters
translate_params["clang"]="algorithm.preproc_strategy=clang generate.max_new_tokens=10000"
translate_params["clang-sys-filter"]="algorithm.preproc_strategy=clang-sys-filter generate.max_new_tokens=10000"

# Set persistent variables
export MODEL="Qwen/Qwen3-Coder-30B-A3B-Instruct"
export VLLM_ARGS="--pipeline-parallel-size 2 --max-model-len 50k"

# Run experiments
for exp in "${experiments[@]}"; do
    echo "Running: ${descriptions[$exp]}..."

    if [ "$need_restart" = true ]; then
        # Start a fresh server
        make kill
        export MODEL="some/model"
        make serve

        # Wait for the server to start
        sleep 5m
    fi

    export TRANSLATION_DIR="${exp}"
    export TRANSLATE_ARGS="${translate_params[$exp]}"
    make examples/clean
    make examples/build

    results["$exp"]=$(find examples -path "*/${TRANSLATION_DIR}/build.log" -size 0 | wc -l)
done

# Pretty print results
echo "=================================================="
echo "               Translation Results                "
echo "=================================================="
for exp in "${experiments[@]}"; do
    printf "%-25s: %2d successful builds\n" "${descriptions[$exp]}" "${results[$exp]}"
done
echo "=================================================="
