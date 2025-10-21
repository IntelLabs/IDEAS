#
# Copyright (C) 2025 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

cmake_minimum_required(VERSION 3.10)
project(hello_world)

add_executable(hello_world src/main.c)
target_include_directories(hello_world PRIVATE include)
