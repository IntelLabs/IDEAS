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
    git \
    lsb-release \
    software-properties-common \
    gnupg \
    vim

# Configuration
ENV CC=clang
ENV UV_INSTALL_DIR="/usr/local/bin"
ENV CODEX_HOME=/opt/codex
ENV CODEX_INSTALL_DIR=/usr/local/bin

# Install all dependencies
COPY INSTALL.mk ./
RUN make -f INSTALL.mk install
RUN rm INSTALL.mk

# Ensure cargo is in the PATH for all users, including non-interactive shells
RUN printf '. "%s/env"\n' "${CARGO_HOME:-/usr/local/cargo}" \
    > /etc/profile.d/cargo.sh && chmod 0644 /etc/profile.d/cargo.sh

# Non-root user
ARG USER_UID=1000
ARG USER_GID=1000
RUN sed -i 's/UID_MAX.*/UID_MAX 20000000/' /etc/login.defs
RUN groupadd -g ${USER_GID} ideas && \
    useradd -m -u ${USER_UID} -g ${USER_GID} user && \
    chown -R user:ideas /home/user && \
    chown -R user:ideas /opt/codex && \
    chown -R user:ideas /usr/local/cargo
USER user
RUN mkdir -p /home/user/IDEAS /home/user/.cache/uv /home/user/.local/share/uv
WORKDIR /home/user/IDEAS

# No nested docker runs; confine steps with Landlock instead
ENV IDEAS_RUNNER="landlock"

# Set up a basic git identity
ENV GIT_AUTHOR_NAME="ideas"
ENV GIT_AUTHOR_EMAIL="ideas@ideas.local"
ENV GIT_COMMITTER_NAME="ideas"
ENV GIT_COMMITTER_EMAIL="ideas@ideas.local"

# Shell quality-of-life for interactive use
COPY --chown=user:ideas docker/ideas.bashrc /home/user/.bashrc

# Codex runs inside Docker, so let it use the container's full filesystem access.
ENV IDEAS_CODEX_SANDBOX="danger-full-access"

# Cache Python dependencies
ENV PYTHONDONTWRITEBYTECODE=1
ENV UV_LINK_MODE="copy"
