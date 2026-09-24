# IDEAS — Improved Decoding and Equivalence Automated testing at Scale
![GitHub License](https://img.shields.io/github/license/IntelLabs/IDEAS)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/IntelLabs/IDEAS/badge)](https://scorecard.dev/viewer/?uri=github.com/IntelLabs/IDEAS)

<div align="center">

[Docker image](#docker-image) ⏐
[Quickstart](#quickstart) ⏐
[Expected C project structure](#expected-c-project-structure) ⏐
[Translated Rust structure](#translated-rust-structure)

[OpenRouter models](#use-with-openrouter-models) ⏐
[OpenAI models](#use-with-openai-models) ⏐
[Locally-hosted models](#use-with-locally-hosted-models)

[C/Rust equivalence test generation](#crust-equivalence-test-generation) ⏐
[Citation](#citation) ⏐
[Acknowledgments](#acknowledgments)

</div>

> [!NOTE]
> IDEAS is under active development and may go through major changes with each release.
> If you encounter any obstacles, please do not hesitate to [open a GitHub issue](https://github.com/IntelLabs/IDEAS/issues/new).


# Docker image
Run `make docker/build` to build the IDEAS Docker image.
All translation steps run inside this image.

## Local development
For local development users must first ensure [`rustup`](https://rustup.rs/) is installed on their machine and available on `PATH`.

Run
```bash
sudo make install-sys
make install-user
```

To install all other required depedencies. Developed and tested on Ubuntu 24.04.

# Quickstart
To translate a C project to a Rust workspace:
- Ensure it uses CMake as a build system.
- Place its top-level `CMakeLists.txt` in the `examples/<your-project-name>/test_case` folder.
- Obtain an [OpenRouter API key](https://openrouter.ai/workspaces/default/keys).

And trigger end-to-end translation using the Docker image:
```bash
make examples/<your-project-name>/translate OPENROUTER_API_KEY="your-key"
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
The translation tool identifies each CMake target (library or executable) and translates it to _three_ separate Rust [crates](https://doc.rust-lang.org/book/ch07-01-packages-and-crates.html#packages-and-crates):

- a `<name>` crate that holds the C FFI compatibility layer for all safe Rust symbols.
- a `<name>-rs` crate that holds the `#![forbid(unsafe_code)]` Rust translation.
- a `<name>-sys` crate that links the original C library through `rust-bindgen` and depletes it as translation progresses.

All crates are organized as a Rust [workspace](https://doc.rust-lang.org/cargo/reference/workspaces.html) in the folder given by the `TRANSLATION_DIR` environment variable.

For example, running
```bash
make docker
TRANSLATION_DIR="translated_rust" make examples/templates/hello_world_lib/translate OPENROUTER_API_KEY="sk-..."
```

Will produce the following translated folder structure:
```
📂examples/templates/hello_world_lib
 ┣ 📂test_case # The original C code is not modified
 ┗ 📂translated_rust
   ┣ 📂libhello_world_lib # C FFI compatibility layer
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

IDEAS is capable of testing Rust translations with the DARPA TRACTOR evaluation schema.
See [here](https://github.com/DARPA-TRACTOR-Program/PUBLIC-Test-Corpus?tab=readme-ov-file#test-vector-schema-json) for more details and the exact specification for writing test vectors and `cando2` runners.

# Use with OpenRouter models
Our translation framework treats [OpenRouter](https://openrouter.ai/) as the default provider.
The `MODEL` environment variable controls which LLM will be used, and should be the model's name on OpenRouter.

# Use with OpenAI models
IDEAS can be used with any OpenAI model by setting the `PROVIDER`, `MODEL`, and `OPENAI_API_KEY` variables:
```bash
TRANSLATION_DIR="translated_rust" make examples/<your-project-name>/translate \
  OPENAI_API_KEY="sk-..." \
  PROVIDER="openai" \
  MODEL="gpt-6-astra"
```

# Use with locally-hosted models
To use with a locally-hosted `vllm` endpoint, simply set `PROVIDER=hosted_vllm` and use the expected model name, for example:
Then, run IDEAS using:
```bash
TRANSLATION_DIR="translated_rust" make examples/<your-project-name>/translate \
  PROVIDER="hosted_vllm" \
  MODEL="Qwen/Qwen3.6-35B-A3B"
```

## Serving locally-hosted models
We offer a single-command launch of locally-hosted models using their [official `vllm` Docker images](https://docs.vllm.ai/en/stable/deployment/docker/#pre-built-images), pinned to an exact version.\
Running:
```bash
make vllm/serve
```

Will serve GLM-5.2 on your local machine using the default recipe for single-instance, 8-way GPU inference.\
Consult the [official `vllm` recipes](https://recipes.vllm.ai/) to identify a model suitable for your local machine.


# C/Rust equivalence test generation
Launch the test generation agent using:
```bash
TRANSLATION_DIR=test_crates make examples/<your-project-name>/testgen \
  TESTGEN_BUDGET="4.0" \
  TESTGEN_STEPS=100
```
Note the `anthropic` prefix is missing from `MODEL` and is instead set as the `PROVIDER`.

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

## Acknowledgments
This material is based upon work supported by the Defense Advanced Research Projects Agency (DARPA) Translating All C To Rust (TRACTOR) program under Agreement No. HR00112590134.
