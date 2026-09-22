#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

BEAR_VERSION = 4.1.5## bear version to install
CMAKE_VERSION = 4.1.2## Cmake version to install
LLVM_VERSION = 21## LLVM version to install
TRANSLATION_TOOLCHAIN = 1.88.0## Rust toolchain to use for IDEAS translation
RUNNER_TOOLCHAIN = 1.94.1## Rust toolchain for building cando runners
CODEX_RELEASE = 0.148.0## Codex version to install
UV_VERSION = 0.11.13## uv version to install
export RUNNER_TOOLCHAIN # Generated tests read it from the environment

.PHONY: install
install:## Install all dependencies
install: | install-sys install-user

.PHONY: install-sys
install-sys:## Install system-wide packages
install-sys: | install-cc install-sys-deps
install-sys-deps: | install-cc

.PHONY: install-user
install-user:## Install user toolchains
install-user: | install-rust install-bear install-uv install-codex
install-bear: | install-rust

.PHONY: install-cc
install-cc:## Install C tooling
	wget https://apt.llvm.org/llvm.sh && \
      chmod +x llvm.sh && \
      ./llvm.sh ${LLVM_VERSION} all && \
      rm ./llvm.sh
	wget https://github.com/Kitware/CMake/releases/download/v${CMAKE_VERSION}/cmake-${CMAKE_VERSION}-linux-x86_64.sh -O /tmp/cmake-install.sh && \
      chmod +x /tmp/cmake-install.sh && \
      /tmp/cmake-install.sh --skip-license --prefix=/usr/local && \
      rm /tmp/cmake-install.sh
	apt-get install -y ninja-build
	ln -sf /usr/bin/clang-${LLVM_VERSION} /usr/bin/clang
	ln -sf /usr/bin/ld.lld-${LLVM_VERSION} /usr/bin/ld.lld

.PHONY: install-rust
install-rust:## Install Rust toolchains and CLI tools
	curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- --default-toolchain ${TRANSLATION_TOOLCHAIN} -y
	rustup default ${TRANSLATION_TOOLCHAIN}
	rustup toolchain install ${RUNNER_TOOLCHAIN}
	rustup component add rustfmt
	rustup component add llvm-tools-preview --toolchain ${TRANSLATION_TOOLCHAIN}-x86_64-unknown-linux-gnu
	cargo install bindgen-cli --version 0.72.1
	cargo install cargo-llvm-cov --version 0.8.6
	cargo install cargo-nextest --version 0.9.114 --locked
	cargo install cargo-insta --version 1.48.0 --locked
	cargo install ast-grep --version 0.45.0 --locked

.PHONY: install-sys-deps
install-sys-deps:## Install miscellaneous dependencies
	apt-get install -y libssl-dev zlib1g-dev libpcre3-dev libpcre2-dev tcl-dev
	apt-get install -y datamash jq ripgrep

.PHONY: install-bear
install-bear:## Install bear ${BEAR_VERSION} from source (requires Rust)
	git clone --branch ${BEAR_VERSION} --depth 1 https://github.com/rizsotto/Bear /tmp/bear
	cd /tmp/bear && cargo build --release && ./scripts/install.sh
	rm -rf /tmp/bear

.PHONY: install-uv
install-uv:## Install uv@${UV_VERSION}
	curl -LsSf https://astral.sh/uv/${UV_VERSION}/install.sh | sh

.PHONY: install-codex
install-codex:## Install codex@${CODEX_RELEASE}
	curl -fsSL https://chatgpt.com/codex/install.sh | CODEX_RELEASE=${CODEX_RELEASE} CODEX_NON_INTERACTIVE=1 sh
