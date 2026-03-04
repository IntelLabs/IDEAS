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
    gnupg

RUN wget https://apt.llvm.org/llvm.sh && \
    chmod +x llvm.sh && \
    ./llvm.sh 21 all

# Install ninja-build
RUN apt-get install -y ninja-build

# Install libssl-dev and dependencies
RUN apt-get install -y \
    zlib1g-dev \
    libssl-dev

# Install specific version of cmake from binary
ARG CMAKE_VERSION=4.1.2
RUN wget https://github.com/Kitware/CMake/releases/download/v${CMAKE_VERSION}/cmake-${CMAKE_VERSION}-linux-x86_64.sh -O /tmp/cmake-install.sh && \
    chmod +x /tmp/cmake-install.sh && \
    /tmp/cmake-install.sh --skip-license --prefix=/usr/local && \
    rm /tmp/cmake-install.sh

# Symlink /usr/bin/clang
RUN ln -s /usr/bin/clang-21 /usr/bin/clang

# Install uv
ENV UV_INSTALL_DIR="/usr/local/bin"
RUN curl -LsSf https://astral.sh/uv/0.9.22/install.sh | sh

# Install Rust toolchain non-interactively
RUN rustup default 1.88.0
RUN rustup component add rustfmt
RUN cargo install bindgen-cli

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
