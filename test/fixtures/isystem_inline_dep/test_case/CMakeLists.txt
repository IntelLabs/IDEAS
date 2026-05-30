cmake_minimum_required(VERSION 3.10)
project(isystem_inline_dep C)

# util shared library - includes util/ with regular -I
add_library(util SHARED util/user.c)
target_include_directories(util PRIVATE util)

# ext shared library - includes util/ with SYSTEM (generates -isystem)
# This is the pattern that causes the bug: same header directory included
# as SYSTEM in one target but regular in another.
add_library(ext SHARED ext/caller.c)
target_include_directories(ext SYSTEM PRIVATE util)
target_include_directories(ext PRIVATE ext)

# Main executable links both
add_executable(isystem_inline_dep main.c)
target_include_directories(isystem_inline_dep PRIVATE util)
target_link_libraries(isystem_inline_dep util ext)
