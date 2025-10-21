# IDEAS — Improved Decoding and Equivalence Automated testing at Scale
![GitHub License](https://img.shields.io/github/license/IntelLabs/IDEAS)
[![OpenSSF Scorecard](https://api.scorecard.dev/projects/github.com/IntelLabs/IDEAS/badge)](https://scorecard.dev/viewer/?uri=github.com/IntelLabs/IDEAS)

## Dependencies
To install the Python and Rust toolchain dependencies, run `make install`.
> [!TIP]
> This will install `uv@0.7.12` and `Rust@1.88.0` for the current user.

## C Project structure
The current toolkit assumes the following structure for the C projects that will be translated:
```
📦IDEAS
 ┣ 📂src/ideas  # Core library
 ┗ 📂examples   # Project folders go here
   ┣ 📂project_name # A single C project folder with an arbitrary name
   ┃ ┣ 📂test_case
   ┃ ┃ ┣ 📂include
   ┃ ┃ ┃ ┗ 📄lib.h
   ┃ ┃ ┣ 📂src
   ┃ ┃ ┃ ┗ 📄main.c
   ┃ ┃ ┗ 📄CMakeLists.txt # Must be correct and complete
   ┃ ┗ 📂test_vectors
   ┃   ┣ 📄test1.json
   ┃   ┗ 📄test2.json
   ┗ 📂other_project_name
```

To run translation on a C project folder, it must be placed in the top-level `examples` folder.

# Basic usage
To start a local vLLM server, run:
```bash
make serve
```
By default, `make serve` without specifying `VLLM_ARGS` is optimized for parallelized **eight-way** inference, so will require eight available devices.

To run LLM-based translation and attempt to build all examples and save results in a newly created `translation.{git-hash}` sub-folder in each folder, run:
```bash
make examples/build
```

To run agentic code repair on existing translations for all examples, run:
```bash
make examples/repair
```

To run all tests (if available) on existing translations for all examples, run:
```bash
make examples/test
```

To control the output directory and translation hyper-parameters, set:
```bash
export TRANSLATION_DIR="custom-name"
export TRANSLATE_ARGS="hyperparam=value"
```

To delete the current set of translated examples, run:
```bash
make examples/clean
```

For translating a specific example using Rust compiler feedback, run:
```bash
make examples/path/to/project/build # Or repair/test/clean for other functionality
```

# Direct usage
To directly call the Python translation module on a specific C translation unit and enable debugging, run:
```bash
uv run python -m ideas.translate filename=/path/to/file.i
```
