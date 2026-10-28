# IDEAS — Improved Decoding and Equivalence Automated testing at Scale
![GitHub License](https://img.shields.io/github/license/IntelLabs/IDEAS)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/IntelLabs/IDEAS/badge)](https://scorecard.dev/viewer/?uri=github.com/IntelLabs/IDEAS)

<div align="center">

[Docker image](#docker-image) ⏐
[Quickstart](#quickstart) ⏐
[Expected C project structure](#expected-c-project-structure) ⏐
[Translated Rust structure](#translated-rust-structure)

[Supported providers and models](#supported-providers-and-models)

[C/Rust equivalence test generation](#crust-equivalence-test-generation) ⏐
[Citation](#citation) ⏐
[Acknowledgments](#acknowledgments)

</div>

> [!NOTE]
> IDEAS is under active development and may go through major changes with each release.
> If you encounter any obstacles, please do not hesitate to [open a GitHub issue](https://github.com/IntelLabs/IDEAS/issues/new).


# Docker image
Run `make docker/build` to build the IDEAS Docker image.
All translation steps run inside this image, started by [`src/ideas/sandbox.py`](src/ideas/sandbox.py).
The host needs [uv](https://docs.astral.sh/uv/) (`make install-uv`) to run IDEAS.

For Ubuntu 24.04 hosts, you can install every dependency with:
```bash
sudo make install-sys
make install-user
```

For other hosts, see [`INSTALL.mk`](INSTALL.mk) for the full dependency list.

## Running with `landlock`
Where Docker is unavailable, e.g. inside an unprivileged container that cannot start another, use `IDEAS_RUNNER=landlock` to keep each step confined to the same files it would see in Docker.
[`sandbox.py`](src/ideas/sandbox.py) applies a [Landlock](https://docs.kernel.org/userspace-api/landlock.html) sandbox:
- IDEAS is read-only,
- `examples/` and the working directory are hidden except for the project under-translation,
- system and toolchain directories are read-only, and
- `HOME` and `TMPDIR` are fresh for each step.
This requires Linux 6.2+ with Landlock enabled and, inside Docker, a seccomp profile that allows it (Docker 23+).

Unlike Docker it does not restrict network access, and the names, permissions, timestamps and extended attributes of paths outside the mounts can still be read or changed (e.g. with `stat`, `chmod` or `touch`).

To open a Landlock-confined shell in this mode, run:
```
make docker
make examples/<project>/docker
```
The IDEAS image sets `IDEAS_RUNNER=landlock`

## Local development
Passing `IDEAS_RUNNER=native` runs translation agents on host.

> [!NOTE]
> This loses sandboxing, so agents could inspect and be biased by previous translation attempts.

# Quickstart
To translate a C project to a Rust workspace:
- Ensure it uses CMake as a build system.
- Place its top-level `CMakeLists.txt` in the `examples/<your-project-name>/test_case` folder.
- Obtain an [OpenRouter API key](https://openrouter.ai/workspaces/default/keys).

And trigger end-to-end translation using the Docker image:
```bash
export OPENROUTER_API_KEY="sk-or-v1-..."
make examples/<your-project-name>/translate
```

# Expected C project structure
To run translation on a C project folder, it must be copied to the top-level `examples` folder.
IDEAS requires the [official DARPA TRACTOR folder structure](https://github.com/DARPA-TRACTOR-Program/PUBLIC-Test-Corpus#test-case-structure) for the C projects that will be translated:
```
📦IDEAS
 ┣ 📂src/ideas  # Core library
 ┗ 📂examples   # Project folders go here
   ┣ 📂your-project-name
   ┃ ┣ 📂test_case
   ┃ ┃ ┣ 📂some-folder
   ┃ ┃ ┣ 📄some-file
   ┃ ┃ ┗ 📄CMakeLists.txt # Must be correct and complete
   ┃ ┗ 📂test_vectors # Optional, must follow the TRACTOR schema
   ┃   ┣ 📄some-test.json
   ┃   ┗ 📄other-test.json
   ┗ 📂other-project-name
```
See the [`examples/templates`](examples/templates/) folder for minimal examples on how to organize your project.

# Translated Rust structure
The translation tool identifies each CMake target (library or executable) and translates it to separate Rust [crates](https://doc.rust-lang.org/book/ch07-01-packages-and-crates.html#packages-and-crates):

- a `<name>-hybrid` crate, internal to translation, that links the C FFI wrappers of translated symbols against the remaining C.
- a `<name>-ffi` crate (libraries only) that exports the C interface of the library from Rust as `lib<name>.so`, linking `<name>-sys` only for the storage of C globals once no C functions remain.
- a `<name>-rs` crate that holds the `#![forbid(unsafe_code)]` Rust translation, and ships the `<name>` binary of an executable.
- a `<name>-sys` crate that links the original C library through `rust-bindgen` and depletes it as translation progresses.

All crates are organized as a Rust [workspace](https://doc.rust-lang.org/cargo/reference/workspaces.html) in the folder given by `TRANSLATION_DIR`.

For example, running
```bash
make docker
export OPENROUTER_API_KEY="sk-or-v1-..."
make examples/templates/hello_world_lib/translate TRANSLATION_DIR="translated_rust"
```

Will produce the following translated folder structure:
```
📂examples/templates/hello_world_lib
 ┣ 📂test_case # The original C code is not modified
 ┗ 📂translated_rust
   ┣ 📂libhello_world_lib-hybrid # C FFI wrappers linked against the remaining C, used during translation
   ┃ ┣ 📂src
   ┃ ┃ ┗ 📄lib.rs
   ┃ ┗ 📄Cargo.toml
   ┣ 📂libhello_world_lib-ffi # C FFI compatibility layer, no C functions
   ┃ ┣ 📂src
   ┃ ┃ ┗ 📄lib.rs
   ┃ ┗ 📄Cargo.toml
   ┣ 📂libhello_world_lib-rs # Safe Rust translation
   ┃ ┣ 📂src
   ┃ ┃ ┗ 📄lib.rs
   ┃ ┗ 📄Cargo.toml
   ┣ 📂libhello_world_lib-sys # Rust bindings to the original C code
   ┃ ┣ 📂src
   ┃ ┃ ┣ 📄lib.c
   ┃ ┃ ┗ 📄lib.rs
   ┃ ┣ 📄build.rs
   ┃ ┗ 📄Cargo.toml
   ┣ 🗄️cache.db # Resumable translation cache
   ┣ 📄Cargo.lock # Workspace lockfile
   ┗ 📄Cargo.toml # Workspace manifest
```

Binary targets have a similar expected crate structure, with a `main.rs` present in all crates.

> [!NOTE]
> If translation fails and exits early, the crates are not guaranteed to be in a valid state, but are still useful for debugging and contain `git` logs that can be inspected.

IDEAS can test completed Rust translations using the DARPA TRACTOR evaluation schema.
See [here](https://github.com/DARPA-TRACTOR-Program/PUBLIC-Test-Corpus?tab=readme-ov-file#test-vector-schema-json) for more details and the exact specification for writing test vectors and `cando2` runners.

# Supported providers and models
## Use with OpenRouter endpoints
[OpenRouter](https://openrouter.ai/) is the default provider.
The `MODEL` variable controls which LLM will be used, and should be the model's name on OpenRouter.

To run LLM-based memory-safe translation of a single project and save the translated Rust workspace in a newly created `TRANSLATION_DIR` sub-folder, run:
```bash
export OPENROUTER_API_KEY="..."
make examples/C-project-name/translate \
  TRANSLATION_DIR="translated_rust" \
  MODEL="openai/gpt-6-luna"
```

If a project (library or executable) was not already found under `TRANSLATION_DIR`, our dependency chain will first trigger its memory-safe translation, followed by C FFI wrappers (only for libraries).

## Use with Microsoft Foundry (formerly Azure AI Studio) deployments
IDEAS can be used with a Microsoft Foundry deployment by setting `PROVIDER` and `BASE_URL`, and exporting `AZURE_OPENAI_API_KEY`:
```bash
export AZURE_OPENAI_API_KEY="..."
make examples/<your-project-name>/translate \
  TRANSLATION_DIR="translated_rust" \
  PROVIDER="azure" \
  BASE_URL="https://<resource>.services.ai.azure.com/openai/v1" \
  MODEL="openai/gpt-6-luna"
```
Ensure that the model name in `MODEL` begins with the vendor name and ends with the [deployment name](https://learn.microsoft.com/en-us/azure/foundry/foundry-models/concepts/models-sold-directly-by-azure?pivots=azure-openai).

> [!CAUTION]
> Currently we only support `openai`-vendored deployments through Microsoft Foundry.

## Use with OpenAI API
IDEAS can be used with any OpenAI model by setting `PROVIDER` and exporting `OPENAI_API_KEY`:
```bash
export OPENAI_API_KEY="..."
make examples/<your-project-name>/translate \
  TRANSLATION_DIR="translated_rust" \
  PROVIDER="openai" \
  MODEL="gpt-6-luna"
```

## Use with locally-hosted vLLM endpoints
We support a single-command launch of locally-hosted models using the [official `vllm` Docker image](https://docs.vllm.ai/en/stable/deployment/docker/#pre-built-images), pinned to an exact version. \
To serve Qwen3.8-27B using the default recipe for single-instance, 8-way GPU inference, run:
```bash
make vllm/serve
```

Consult the [official `vllm` recipes](https://recipes.vllm.ai/) to identify a model suitable for your platform. \
Run IDEAS against the locally-hosted endpoint by setting `PROVIDER`, `BASE_URL`, and `MODEL`:
```bash
export VLLM_API_KEY="local"
make examples/<your-project-name>/translate \
  TRANSLATION_DIR="translated_rust" \
  PROVIDER="vllm" \
  BASE_URL="http://host.docker.internal:8000/v1" \
  MODEL="Qwen/Qwen3.8-27B"
```

> [!NOTE]
> Unless vLLM was started with `--api-key` it does not require `VLLM_API_KEY` but its value be non-empty since Codex rejects a provider whose auth command returns an empty token.

## Use with other Responses API-compatible providers
IDEAS could also be used with any other endpoint that serves a Responses API and support key-based authentication by setting `PROVIDER=vllm` and `BASE_URL` to the HTTP endpoint, and exporting `VLLM_API_KEY`.

> [!NOTE]
> Response API providers and deployments not explicitly listed above may not work out of the box.

## Use with Anthropic models
Because Anthropic does not serve a Responses API, the only reliable way to use Anthropic models in IDEAS is currently through an [OpenRouter endpoint](#use-with-openrouter-endpoints).

# C/Rust equivalence test generation
Launch the test generation agent using:
```bash
make examples/<your-project-name>/testgen \
  TRANSLATION_DIR=test_crates \
  TESTGEN_BUDGET="4.0" \
  TESTGEN_STEPS=100
```
Note the `anthropic` prefix is missing from `MODEL` and is instead set as the `PROVIDER`.

`TESTGEN_MODEL` defaults to `${MODEL}`.
Set it explicitly to generate tests with one model while translating with another.

This is a monolithic (single-session) agent tasked with generating unit and integration tests with high branch coverage.
You can consult the agent prompt [here](src/ideas/agents/generate_io_tests.py).

For every target, the agent writes two test files to the `*-sys` crate:
- `tests/collect.rs`, which snapshots inputs and matching outputs from the C code into `json` files.
- `tests/io.rs`, which asserts recorded snapshots using Rust's `assert!` and `assert_eq!` macros.

The full agent trajectory is logged next to them as `testgen-*.log`.

# Citation
If you find this repository useful, please cite the following work:
```
@ARTICLE{11635938,
  author={Cornelius, Cory and Melara, Marcela S. and Xu, Weilin and Arvinte, Marius and Momeu, Marius and He, Jingxuan and Sen, Koushik and Song, Dawn},
  journal={IEEE Security & Privacy},
  title={IDEAS: C-to-Rust Translation Using Improved Large Language Model Decoding and Automated Equivalence Testing},
  year={2026},
  volume={},
  number={},
  pages={2-11},
  keywords={Testing;Translation;Codes;Memory;Modeling;Symbols;Feedback;Large language models;Security;Safety},
  doi={10.1109/MSEC.2026.3710126}}
```

If you found the idiomaticity scoring rubric useful, please cite the corresponding MIT-LL work:
```
@article{okhravi2026tractor,
  title={TRACTOR Benchmark for Evaluating C to Rust Translators},
  author={Okhravi, Hamed and Ogden, Brandt and Luther, Noah and McQuoid, Ian and Mak, Howard and Burow, Nathan},
  journal={arXiv preprint arXiv:2609.25121},
  year={2026}
}
```

## Acknowledgments
This material is based upon work supported by the Defense Advanced Research Projects Agency (DARPA) Translating All C To Rust (TRACTOR) program under Agreement No. HR00112590134.
