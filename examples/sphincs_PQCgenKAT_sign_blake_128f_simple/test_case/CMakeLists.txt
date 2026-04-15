cmake_minimum_required(VERSION 3.10)
project(SPHINCS_PLUS C)

# Parameters for hash backend
set(HASH_BACKEND "haraka" CACHE STRING "Hash backend (haraka, sha2, shake256, blake)")
set(THASH "robust" CACHE STRING "thash variant (robust, simple)")
set(SECPAR "128s" CACHE STRING "hash parameters (secpar(s|f)")

set(CMAKE_C_STANDARD 99)
find_program(CLANG_EXECUTABLE clang)
if(CLANG_EXECUTABLE)
    set(CMAKE_C_COMPILER ${CLANG_EXECUTABLE})
endif()
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -O3")

# Add subdirectories
add_subdirectory(lib)
add_subdirectory(app)
