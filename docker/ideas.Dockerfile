#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#


FROM docker.io/rust:bookworm

RUN apt-get update && apt-get install -y \
    build-essential \
    checkinstall \
    pkg-config \
    jq \
    git \
    lsb-release \
    software-properties-common \
    gnupg \
    vim

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

# Non-root user
ARG USER_UID=1000
ARG USER_GID=1000
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

# Configure Python and uv
ENV PYTHONDONTWRITEBYTECODE=1
ENV UV_LINK_MODE="copy"

# Shell quality-of-life for interactive use
COPY --chown=user:ideas ideas.bashrc /home/user/.bashrc
