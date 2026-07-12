# IDEAS — Improved Decoding and Equivalence Automated testing at Scale
![GitHub License](https://img.shields.io/github/license/IntelLabs/IDEAS)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/IntelLabs/IDEAS/badge)](https://scorecard.dev/viewer/?uri=github.com/IntelLabs/IDEAS)

> [!NOTE]
> IDEAS is a framework under active development which may go through major changes with each release.  
> If you encounter any issues or have questions about how to run the framework, please do not hesitate to [open a GitHub issue](https://github.com/IntelLabs/IDEAS/issues/new).

# Requirements
Tested on Ubuntu 24.04.

To install the Python and Rust toolchain dependencies, run `make install` and follow the prompts if requested.
> [!TIP]
> This will install `uv@0.9.22` and `rust@1.88.0` for the current user.

# Quick start
To translate a single C project to a Rust workspace, ensure it uses Cmake as a build system, place it in the `examples` folder and run:
```bash
make examples/C-project-name/translate \
  OPENROUTER_API_KEY="your-key" \
  MODEL="openai/gpt-5.1" \
  TRANSLATION_TEST=smoke
```

# Expected C project structure
To run translation on a C project folder, it must be placed in the top-level `examples` folder.  
The toolkit assumes the [official DARPA TRACTOR folder structure](https://github.com/DARPA-TRACTOR-Program/PUBLIC-Test-Corpus#test-case-structure) for the C projects that will be translated:
```
📦IDEAS
 ┣ 📂src/ideas  # Core library
 ┗ 📂examples   # Project folders go here
   ┣ 📂C-project-name # A single C project folder with an arbitrary name
   ┃ ┣ 📂test_case
   ┃ ┃ ┣ 📂include
   ┃ ┃ ┣ 📂src
   ┃ ┃ ┗ 📄CMakeLists.txt # Must be correct and complete
   ┃ ┗ 📂test_vectors
   ┃   ┣ 📄some-name.json
   ┃   ┗ 📄other-name.json
   ┗ 📂other-C-project-name
```
This follows the standard evaluation convention in the DARPA TRACTOR project, which can be consulted in more detail [here](https://github.com/DARPA-TRACTOR-Program/PUBLIC-Test-Corpus) (including many C projects in `examples`).

# Translated Rust structure
The translation tool identifies each Cmake target (library or binary), and translates it to a separate, self-contained Rust [crate](https://doc.rust-lang.org/book/ch07-01-packages-and-crates.html#packages-and-crates). All crates are organized together in a Rust [workspace](https://doc.rust-lang.org/cargo/reference/workspaces.html) under the folder given by the `TRANSLATION_DIR` environment variable, alongside the original C `test_case` folder:

```
📂C-project-name # A single C project folder with an arbitrary name
 ┣ 📂test_case
 ┣ 📂test_vectors
 ┗ 📂${TRANSLATION_DIR}
   ┣ 📂target-1
   ┃ ┣📂src
   ┃ ┃ ┣📄lib.rs / main.rs # For libraries / executables
   ┃ ┃ ┣📄wrapper.rs # Wrapper module (libraries only)
   ┃ ┃ ┗📂wrapper # Individual wrappers for every exported C symbol (libraries only)
   ┃ ┗📄Cargo.toml # For crate (translated C target)
   ┣ 📂target-2
   ┗ 📄Cargo.toml # For workspace
```

Inside each translated crate, there are at most **two** distinct components of the Rust translation:
1. A consolidated `lib.rs` or `main.rs` file containing the **guaranteed** memory-safe, attempted Rust translation.
2. (only for library targets/crates) A `wrapper` directory containing C FFI wrappers for exact backwards compatibility with the original C library.

The translation tool is also capable of testing the individual Rust crates in a workspace, but the DARPA TRACTOR evaluation schema must be followed by the `.json` files. See [here](https://github.com/DARPA-TRACTOR-Program/PUBLIC-Test-Corpus?tab=readme-ov-file#test-vector-schema-json) for more details and the exact specification.

# Docker image
A docker image with the user-specific name `ideas-${UID}` can be built and directly entered using:
```bash
make docker
```

This allows for isolated execution in a reproducible environment.

> [!TIP]
> If the `OPENROUTER_API_KEY` environment variable is set on the host, it will be automatically passed to the interactive session.

# Usage with OpenRouter API
Our translation framework treats [OpenRouter](https://openrouter.ai/) as the default provider, allowing easy switching between models.
The `MODEL` environment variable controls which LLM will be used, and should be the model's name on OpenRouter.

To run LLM-based memory-safe translation of a single project and save the translated Rust workspace in a newly created `TRANSLATION_DIR` sub-folder run:
```bash
make examples/C-project-name/translate \
  TRANSLATION_DIR="translated_rust" \
  OPENROUTER_API_KEY="your-key" \
  MODEL="openai/gpt-5.1" \
  TRANSLATION_TEST=smoke
```

If a project (library or executable) was not already found under `TRANSLATION_DIR`, our dependency chain will first trigger its memory-safe translation, followed by C FFI wrappers (only for libraries).

# Usage with Anthropic API
IDEAS can be used with any Anthropic model by setting the `PROVIDER`, `MODEL`, and `ANTHROPIC_API_KEY` variables:
```bash
make examples/C-project-name/translate \
  TRANSLATION_DIR="translated_rust" \
  ANTHROPIC_API_KEY="your-key" \
  PROVIDER="anthropic" \
  MODEL="claude-sonnet-4.6" \
  TRANSLATION_TEST=smoke
```
Note the prefix `anthropic` is missing from `MODEL`, and must instead be set as the `PROVIDER`.

# Usage with OpenAI API
IDEAS can be used with any OpenAI model by setting the `PROVIDER`, `MODEL`, and `OPEN_API_KEY` variables:
```bash
make examples/C-project-name/translate \
  TRANSLATION_DIR="translated_rust" \
  OPEN_API_KEY="your-key" \
  PROVIDER="openai" \
  MODEL="gpt-5.5" \
  TRANSLATION_TEST=smoke
```
Note the prefix `anthropic` is missing from `MODEL`, and must instead be set as the `PROVIDER`.

# Usage with other APIs
IDEAS relies on [`litellm`](https://github.com/BerriAI/litellm), which supports many other model providers (e.g., Google Vertex, MS Azure, etc).

The instructions at https://docs.litellm.ai/docs/providers inform which parameters should be set in `litellm` and IDEAS flows through the [`dspy.LM`](https://dspy.ai/api/models/LM/) instance.

We are working on writing a comprehensive guide for more providers, but developers can inspect [how the `dspy.LM` is instantiated by IDEAS](https://github.com/IntelLabs/IDEAS/blob/main/src/ideas/model.py) and infer any additional required parameters that need to be passed to `litellm`.

# Running TRACTOR tests
To run all tests (if available and following the [DARPA TRACTOR evaluation schema](https://github.com/DARPA-TRACTOR-Program/PUBLIC-Test-Corpus#test-vector-schema-json)) on an existing translation, run:
```bash
make examples/C-project-name/test
```

If a project was not already translated, this will trigger complete translation and testing, and can be used a **single-click** translation-and-evaluation command.

To translate, test, and aggregate statistics about all projects contained in the `examples` folder, run:
```bash
make -j128 examples/test \
  VERBOSE=1
  ...
```
> [!TIP]
> The 128-way parallelism will be CPU-intensive, and can be reduced if needed.

## Acknowledgments
This material is based upon work supported by the Defense Advanced Research Projects Agency (DARPA) under Agreement No. HR00112590134.
