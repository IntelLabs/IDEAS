#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


FROM docker.io/rust:bookworm@sha256:77fac8b98f9f46062bb680b6d25d5bcaabfc400143952ebc572e924bcbedc3fa

RUN apt-get update && apt-get install -y \
    build-essential \
    checkinstall \
    pkg-config \
    jq \
    git \
    lsb-release \
    software-properties-common \
    gnupg \
    vim \
    datamash

RUN wget https://apt.llvm.org/llvm.sh && \
    chmod +x llvm.sh && \
    ./llvm.sh 21 all && \
    rm ./llvm.sh

# Install ninja-build
RUN apt-get install -y ninja-build

# Install possible translation dependencies
RUN apt-get install -y \
    zlib1g-dev \
    libssl-dev \
    libpcre3-dev

# Install specific version of cmake from binary
ARG CMAKE_VERSION=4.1.2
RUN wget https://github.com/Kitware/CMake/releases/download/v${CMAKE_VERSION}/cmake-${CMAKE_VERSION}-linux-x86_64.sh -O /tmp/cmake-install.sh && \
    chmod +x /tmp/cmake-install.sh && \
    /tmp/cmake-install.sh --skip-license --prefix=/usr/local && \
    rm /tmp/cmake-install.sh

# Symlink /usr/bin/clang and set it as default compiler
RUN ln -s /usr/bin/clang-21 /usr/bin/clang
ENV CC=clang
# Symlink /usr/bin/ld.lld because Bear's intercept-preload needs lld
RUN ln -s /usr/bin/ld.lld-21 /usr/bin/ld.lld

# Install uv
ENV UV_INSTALL_DIR="/usr/local/bin"
RUN curl -LsSf https://astral.sh/uv/0.11.13/install.sh | sh

# Install Rust toolchain non-interactively
RUN rustup default 1.88.0
RUN rustup component add rustfmt
RUN rustup component add llvm-tools-preview --toolchain 1.88.0-x86_64-unknown-linux-gnu
RUN cargo install bindgen-cli --version 0.72.1
RUN cargo install cargo-llvm-cov --version 0.8.6
RUN cargo install cargo-nextest --version 0.9.114 --locked

# Install Bear (Build EAR) for capturing build commands
ARG BEAR_VERSION=4.1.5
RUN git clone --branch ${BEAR_VERSION} --depth 1 https://github.com/rizsotto/Bear /tmp/bear
RUN cd /tmp/bear && cargo build --release && ./scripts/install.sh
RUN rm -rf /tmp/bear

# Non-root user
ARG USER_UID=1000
ARG USER_GID=1000
RUN sed -i 's/UID_MAX.*/UID_MAX 20000000/' /etc/login.defs
RUN groupadd -g ${USER_GID} ideas && \
    useradd -m -u ${USER_UID} -g ${USER_GID} user && \
    chown -R user:ideas /home/user && \
    chown -R user:ideas /usr/local/cargo
USER user
RUN mkdir -p /home/user/IDEAS
WORKDIR /home/user/IDEAS

# Set up a basic git identity
ENV GIT_AUTHOR_NAME="ideas"
ENV GIT_AUTHOR_EMAIL="ideas@ideas.local"
ENV GIT_COMMITTER_NAME="ideas"
ENV GIT_COMMITTER_EMAIL="ideas@ideas.local"

# Shell quality-of-life for interactive use
COPY --chown=user:ideas ideas.bashrc /home/user/.bashrc

# Cache Python dependencies
ENV PYTHONDONTWRITEBYTECODE=1
ENV UV_LINK_MODE="copy"
COPY pyproject.toml uv.lock .
RUN uv sync --frozen --no-install-project && rm -rf pyproject.toml uv.lock .venv

# Cache cargo dependencies
RUN cargo init --lib cargo_deps && \
    cd cargo_deps && \
    cargo add libc@0.2.185 \
              openssl@0.10.79 \
              flate2@1 \
              regex@1 && \
    cargo add --dev \
              assert_cmd@2.0.17 \
              predicates@3.1.3 \
              once_cell@1.21.3 \
              test-cdylib@1.1.0 \
              serde_json@1 \
              tempfile@3 && \
    cargo add --dev \
              --features derive serde@1 && \
    cargo add --build \
              cc@1.2.53 && \
    CARGO_NET_OFFLINE=false cargo metadata \
              --manifest-path /home/user/IDEAS/cargo_deps/Cargo.toml
