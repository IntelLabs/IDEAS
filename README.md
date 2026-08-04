# IDEAS — Improved Decoding and Equivalence Automated testing at Scale
![GitHub License](https://img.shields.io/github/license/IntelLabs/IDEAS)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/IntelLabs/IDEAS/badge)](https://scorecard.dev/viewer/?uri=github.com/IntelLabs/IDEAS)

> [!NOTE]
> IDEAS is a framework under active development which may go through major changes with each release.
> If you encounter any issues or have questions about how to run the framework, please do not hesitate to [open a GitHub issue](https://github.com/IntelLabs/IDEAS/issues/new).

## Controlling translation costs on large-scale projects
> [!NOTE]
> The default C FFI wrapper generation context does not include already-generated C FFI wrappers.
> This can reduce the performance of the translator on projects with, e.g., function pointers, where a C FFI wrapper _must_ call others (without major logic duplication).

> To enable full C FFI wrapper generation context, set the `REDUCED_CONTEXT=0` environment variable:
```bash
REDUCED_CONTEXT=0 make examples/C-project-name/translate
```
> [!CAUTION]
> This is currently experimental and can lead to prohibitive costs and exceeding input context limits for large-scale, fragmented C projects (20k+ LoC).


# Requirements
Developed and tested on Ubuntu 24.04.

IDEAS requires specific versions of the `clang` and Rust toolchains to translate C to Rust.
A Docker image with the user-specific name `ideas-${UID}` can be built using:
```bash
make docker/build
```

We strongly recommend launching all runs in the Docker image.

> [!NOTE]
> Setting the `TRANSLATION_DIR` environment is **mandatory** when mounting examples to the Docker image.  
> If the `OPENROUTER_API_KEY` or `OPENAI_API_KEY` environment variables are set on the host, they will be automatically passed to the interactive session.

# Quickstart
To translate a single C project to a Rust workspace, ensure it uses CMake as a build system, place it in the `examples` folder, and generate an [OpenRouter API key](https://openrouter.ai/workspaces/default/keys).

Then, build and mount your project and API key in an interactive Docker session:
```bash
TRANSLATION_DIR="translation.demo" OPENROUTER_API_KEY="your-key" make examples/C-project-name/docker 
```

And trigger end-to-end translation:
```bash
make examples/C-project-name/translate
```

# Expected C project structure
To run translation on a C project folder, it must be copied to the top-level `examples` folder.
IDEAS requires the [official DARPA TRACTOR folder structure](https://github.com/DARPA-TRACTOR-Program/PUBLIC-Test-Corpus#test-case-structure) for the C projects that will be translated:
```
📦IDEAS
 ┣ 📂src/ideas  # Core library
 ┗ 📂examples   # Project folders go here
   ┣ 📂C-project-name
   ┃ ┣ 📂test_case
   ┃ ┃ ┣ 📂include
   ┃ ┃ ┣ 📂src
   ┃ ┃ ┗ 📄CMakeLists.txt # Must be correct and complete
   ┃ ┗ 📂test_vectors
   ┃   ┣ 📄some-name.json
   ┃   ┗ 📄other-name.json
   ┗ 📂other-C-project-name
```
See the [`examples/templates`](examples/templates/) folder for minimal examples.

# Translated Rust structure
The translation tool identifies each CMake target (library or binary) and currently translates it to _three_ separate, self-contained Rust [crates](https://doc.rust-lang.org/book/ch07-01-packages-and-crates.html#packages-and-crates):

- a `*` crate that holds C FFI compatibility wrappers.
- a `*-rs` crate holding the guaranteed-safe Rust translation.
- a `*-sys` crate that links the original C library.

All crates are organized under a Rust [workspace](https://doc.rust-lang.org/cargo/reference/workspaces.html) in the folder given by the `TRANSLATION_DIR` environment variable, alongside the original C `test_case` folder.

For example, running
```bash
TRANSLATION_DIR="translation.demo" OPENROUTER_API_KEY="sk-..." make examples/docker
make examples/templates/hello_world_lib/translate
```

Should produce the following translated folder structure:
```
📂examples/templates/hello_world_lib
 ┣ 📂test_case
 ┗ 📂translation.demo
   ┣ 📂libhello_world_lib # Safe Rust + C FFI-compatible wrappers
   ┃ ┣ 📂src
   ┃ ┃ ┣ 📄lib.rs
   ┃ ┃ ┗ 📄wrap_hello_print.rs # `wrap_{name}`: C FFI compatibility wrapper
   ┃ ┣ 📂tests
   ┃ ┃ ┗ 📄smoke.rs # Always-passing tests by default
   ┃ ┗ 📄Cargo.toml
   ┣ 📂libhello_world_lib-rs # Guaranteed safe Rust translation
   ┃ ┣ 📂src
   ┃ ┃ ┗ 📄lib.rs
   ┃ ┗ 📄Cargo.toml
   ┣ 📂libhello_world_lib-sys # Links C/Rust along the translation trajectory
   ┃ ┣ 📂src
   ┃ ┃ ┣ 📄lib.c
   ┃ ┃ ┗ 📄lib.rs
   ┃ ┣ 📂tests
   ┃ ┃ ┗ 📄smoke.rs # Always-passing tests by default
   ┃ ┣ 📄build.rs # Hybrid build script
   ┃ ┗ 📄Cargo.toml
   ┣ 🗄️cache.db # Resumable translation cache
   ┣ 📄Cargo.lock # Workspace lockfile
   ┗ 📄Cargo.toml # Workspace manifest
```

For a library target named `hello_world_lib`, the three generated crates play the following roles:

- **`libhello_world_lib`** — the C FFI-compatible translated crate. By default, it contains a (possibly unsafe) wrapper `wrap_{name}` for every symbol (including private ones) that wraps the safe Rust translation and restores C FFI compatibility.
- **`libhello_world_lib-rs`** — the guaranteed-safe Rust translation of the original C target. It enforces `#![forbid(unsafe_code)]` at the top of each module.
- **`libhello_world_lib-sys`** — the consolidated C code visible through the Rust C FFI. As symbols are translated, they are progressively depleted from the source file until it can be dropped entirely on complete translation.

Binary targets have a similar expected crate structure, with a `main.rs` present in all crates.

> [!NOTE]
> If translation fails and exits early, the crates are not guaranteed to be in a valid state, but are still useful for debugging and contain exact `git` logs.

IDEAS is capable of testing Rust translations with the DARPA TRACTOR evaluation schema.
See [here](https://github.com/DARPA-TRACTOR-Program/PUBLIC-Test-Corpus?tab=readme-ov-file#test-vector-schema-json) for more details and the exact specification for writing test vectors and `cando2` runners.

# Usage with OpenRouter API
Our translation framework treats [OpenRouter](https://openrouter.ai/) as the default provider, allowing easy switching between models.
The `MODEL` environment variable controls which LLM will be used, and should be the model's name on OpenRouter.

To run LLM-based memory-safe translation of a single project and save the translated Rust workspace in a newly created `TRANSLATION_DIR` sub-folder, run:
```bash
TRANSLATION_DIR="translation.demo" OPENROUTER_API_KEY="sk-..." make examples/C-project-name/docker
make examples/C-project-name/translate 
```

If a project (library or executable) was not already found under `TRANSLATION_DIR`, our dependency chain will first trigger its memory-safe translation, followed by C FFI wrappers (only for libraries).

# Usage with Anthropic API
IDEAS can be used with any Anthropic model by setting the `PROVIDER`, `MODEL`, and `ANTHROPIC_API_KEY` variables:
```bash
TRANSLATION_DIR="translation.demo" ANTHROPIC_API_KEY="sk-..." make examples/C-project-name/docker
make examples/C-project-name/translate \
  PROVIDER="anthropic" \
  MODEL="claude-sonnet-4.6"
```
Note the `anthropic` prefix is missing from `MODEL` and is instead set as the `PROVIDER`.

# Usage with OpenAI API
IDEAS can be used with any OpenAI model by setting the `PROVIDER`, `MODEL`, and `OPENAI_API_KEY` variables:
```bash
TRANSLATION_DIR="translation.demo" OPENAI_API_KEY="sk-..." make examples/C-project-name/docker
make examples/C-project-name/translate \
  PROVIDER="openai" \
  MODEL="gpt-5.4"
```
Note the `openai` prefix is missing from `MODEL` and is instead set as the `PROVIDER`.

# Usage with other APIs
IDEAS relies on [`litellm`](https://github.com/BerriAI/litellm), which supports many other model providers (e.g., Google Vertex, MS Azure, etc.).

The instructions at https://docs.litellm.ai/docs/providers indicate which parameters should be set in `litellm`; IDEAS passes them through the [`dspy.LM`](https://dspy.ai/api/models/LM/) instance.

Developers can inspect [how a `dspy.LM` is instantiated by IDEAS](https://github.com/IntelLabs/IDEAS/blob/main/src/ideas/model.py) and infer any additional parameters that need to be passed to `litellm`.

# Usage with locally-hosted models
We support a single-command launch of locally-hosted models using the [official `vllm` Docker image](https://docs.vllm.ai/en/stable/deployment/docker/#pre-built-images), pinned to an exact version.

Running
```bash
make vllm/serve
```

will serve GLM-5.2 on your machine using the default recipe for single-instance, 8-way GPU inference.

Consult the [official `vllm` recipes](https://recipes.vllm.ai/) to identify a model suitable for your platform.
For example, to use the [Qwen3.6-35B-A3B](https://recipes.vllm.ai/Qwen/Qwen3.6-35B-A3B) model, set the `VLLM_RECIPE` Makefile variable to its recipe and directly serve the model:
```bash
make VLLM_RECIPE=vllm serve Qwen/Qwen3.6-35B-A3B \
  --trust-remote-code \
  --tensor-parallel-size 1 \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_xml \
  --reasoning-parser qwen3 \
  --mm-encoder-tp-mode data vllm/serve
```

Then, run IDEAS on host using:
```bash
TRANSLATION_DIR="translation.demo" make examples/C-project-name/translate \
  PROVIDER="hosted_vllm" \
  MODEL="Qwen/Qwen3.6-35B-A3B"
```

# (Experimental) C/Rust equivalence test generation
IDEAS has a submodule that automatically generates portable Rust C FFI tests for libraries and binary targets.
`TRANSLATION_DIR` is overloaded to also hold the test generation results, so the same directory is used whether or not a translation was already produced in it.

First, mount the project into the Docker image:
```bash
TRANSLATION_DIR="translation.demo" OPENROUTER_API_KEY="sk-..." make examples/templates/hello_world_lib/docker
```

This mounts the repository read-only and bind-mounts only `test_case` and `TRANSLATION_DIR` as writable, so the agent cannot reach the rest of the host.
Use `make examples/docker` instead to mount every project in `EXAMPLES` at once.

Then, from the interactive session, launch the test generation agent:
```bash
make examples/templates/hello_world_lib/testgen \
  TESTGEN_BUDGET="4.0" \
  TESTGEN_COVERAGE="100"
```
Note the `anthropic` prefix is missing from `MODEL` and is instead set as the `PROVIDER`.

This launches a self-sufficient [KISS](https://github.com/ksenxx/kiss_ai) agent that is tasked with generating unit and integration tests with high branch coverage.
You can consult the agent prompt [here](src/ideas/agents/generate_io_tests.py).

For every target, the agent writes two test files to the `*-sys` crate: `tests/collect.rs`, which records the behavior of the C code into `json/`, and `tests/io.rs`, which asserts that recorded behavior through the C FFI.
The full agent trajectory is logged next to them as `testgen-*.log`.

> [!NOTE]
> This behavior is disabled by default and may not be reliable on large codebases and/or with weaker LLMs.

## Acknowledgments
This material is based upon work supported by the Defense Advanced Research Projects Agency (DARPA) Translating All C To Rust (TRACTOR) program under Agreement No. HR00112590134.
