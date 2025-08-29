#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

cmake_minimum_required(VERSION 3.10)
project(hello_fancy)

add_executable(hello_fancy src/main.c)
target_include_directories(hello_fancy PRIVATE include)
