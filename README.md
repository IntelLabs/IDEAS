# IDEAS — Improved Decoding and Equivalence Automated testing at Scale
![GitHub License](https://img.shields.io/github/license/IntelLabs/IDEAS)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/IntelLabs/IDEAS/badge)](https://scorecard.dev/viewer/?uri=github.com/IntelLabs/IDEAS)

> [!NOTE]
> IDEAS is a framework under active development which may go through major changes with each release.
> If you encounter any issues or have questions about how to run the framework, please do not hesitate to [open a GitHub issue](https://github.com/IntelLabs/IDEAS/issues/new).

# Requirements
Developed and tested on Ubuntu 24.04.

IDEAS requires a specific version of `clang` and Rust toolchains to translate C-to-Rust.
A docker image with the user-specific name `ideas-${UID}` can be built and launched in an interactive session using:
```bash
make docker
```

We strongly recommend launching all runs in the Docker image.

> [!NOTE]
> If the `OPENROUTER_API_KEY` or `OPENAI_API_KEY` environment variables are set on the host, they will be automatically passed to the interactive session.

# Quickstart
To translate a single C project to a Rust workspace, ensure it uses Cmake as a build system, place it in the `examples` folder and generate an [OpenRouter API key](https://openrouter.ai/workspaces/default/keys).

Then, build and launch the official Docker image:
```bash
make docker
```

And trigger end-to-end translation:
```bash
make examples/C-project-name/translate OPENROUTER_API_KEY="your-key"
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
The translation tool identifies each Cmake target (library or binary), and translates it to a separate, self-contained Rust [crate](https://doc.rust-lang.org/book/ch07-01-packages-and-crates.html#packages-and-crates).
Crates are organized together in a Rust [workspace](https://doc.rust-lang.org/cargo/reference/workspaces.html) under the folder given by the `TRANSLATION_DIR` environment variable, alongside the original C `test_case` folder.

For example, running
```bash
make docker
make examples/templates/hello_world_lib/translate TRANSLATION_DIR="translation.demo" OPENROUTER_API_KEY="your-key"
```

Should produce the following translated folder structure:
```
📂examples/templates/hello_world_lib
 ┣ 📂test_case
 ┣ 📂test_vectors
 ┗ 📂translation.demo
   ┣ 📂hello_world_lib
   ┃ ┣ 📂src
   ┃ ┃ ┣ 📄lib.c # Stub C library
   ┃ ┃ ┣ 📄lib.rs # Translated Rust library
   ┃ ┃ ┣ 📄wrapper.rs # Wrapper module
   ┃ ┃ ┗ 📂wrapper # Per-symbol C FFI compatibility wrappers for all symbols
   ┃ ┣ 📂tests
   ┃ ┃ ┗ 📄 smoke.rs # No tests by default
   ┃ ┗ 📄Cargo.toml # Crate manifest
   ┣ 📄build.rs # Hybrid build script
   ┣ 🗄️cache.db # Resumable translation cache
   ┣ 📄Cargo.lock # Workspace lockfile
   ┗ 📄Cargo.toml # Workspace manifest
```

IDEAS is capable of testing Rust translations with the DARPA TRACTOR evaluation schema.
See [here](https://github.com/DARPA-TRACTOR-Program/PUBLIC-Test-Corpus?tab=readme-ov-file#test-vector-schema-json) for more details and the exact specification for writing test vectors and `cando2` runners.

# Usage with OpenRouter API
Our translation framework treats [OpenRouter](https://openrouter.ai/) as the default provider, allowing easy switching between models.
The `MODEL` environment variable controls which LLM will be used, and should be the model's name on OpenRouter.

To run LLM-based memory-safe translation of a single project and save the translated Rust workspace in a newly created `TRANSLATION_DIR` sub-folder run:
```bash
make examples/C-project-name/translate \
  TRANSLATION_DIR="translated_rust" \
  OPENROUTER_API_KEY="your-key" \
```

If a project (library or executable) was not already found under `TRANSLATION_DIR`, our dependency chain will first trigger its memory-safe translation, followed by C FFI wrappers (only for libraries).

# Usage with Anthropic API
IDEAS can be used with any Anthropic model by setting the `PROVIDER`, `MODEL`, and `ANTHROPIC_API_KEY` variables:
```bash
make examples/C-project-name/translate \
  TRANSLATION_DIR="translated_rust" \
  ANTHROPIC_API_KEY="your-key" \
  PROVIDER="anthropic" \
  MODEL="claude-sonnet-4.6"
```
Note the `anthropic` prefix is missing from `MODEL` and is instead set as the `PROVIDER`.

# Usage with OpenAI API
IDEAS can be used with any OpenAI model by setting the `PROVIDER`, `MODEL`, and `OPEN_API_KEY` variables:
```bash
make examples/C-project-name/translate \
  TRANSLATION_DIR="translated_rust" \
  OPEN_API_KEY="your-key" \
  PROVIDER="openai" \
  MODEL="gpt-5.4"
```
Note the `openai` prefix is missing from `MODEL` and is instead set as the `PROVIDER`.

# Usage with other APIs
IDEAS relies on [`litellm`](https://github.com/BerriAI/litellm), which supports many other model providers (e.g., Google Vertex, MS Azure, etc).

The instructions at https://docs.litellm.ai/docs/providers inform which parameters should be set in `litellm` and IDEAS flows through the [`dspy.LM`](https://dspy.ai/api/models/LM/) instance.

Developers can inspect [how a `dspy.LM` is instantiated by IDEAS](https://github.com/IntelLabs/IDEAS/blob/main/src/ideas/model.py) and infer any additional required parameters that need to be passed to `litellm`.

# (Experimental) C/Rust equivalence test generation
IDEAS has a submodule that automatically generates portable Rust C FFI tests for libraries and binary targets.
To directly launch the test generation agent in an isolated enviroment run (from host):

```bash
make examples/templates/hello_world_lib/testgen_agent OPENROUTER_API_KEY="your-key"
```

This launches a self-sufficient KISS agent that is tasked with generating unit and integration tests with high branch coverage.
You can consult the agent prompt (for libraries; similar for binaries) [here](src/ideas/agents/testgen.py).

> [!NOTE]
> This behavior is disabled by default and may not be reliable on large codebases and/or weaker LLMs.

## Acknowledgments
This material is based upon work supported by the Defense Advanced Research Projects Agency (DARPA) under Agreement No. HR00112590134.
