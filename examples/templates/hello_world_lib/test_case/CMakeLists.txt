#
# Copyright (C) 2026 Intel Corporation
#
# SPDX-License-Identifier: Apache-2.0
#

cmake_minimum_required(VERSION 3.10)
cmake_path(GET CMAKE_CURRENT_SOURCE_DIR PARENT_PATH parent)
cmake_path(GET parent FILENAME project_name)
project(${project_name})

add_library(${project_name} SHARED src/hello.c)
target_include_directories(${project_name} PUBLIC include)
