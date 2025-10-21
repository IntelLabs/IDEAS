cmake_minimum_required(VERSION 3.20)

# https://stackoverflow.com/questions/60211516/programmatically-get-all-targets-in-a-cmake-project
function(get_all_targets _result _dir)
    get_property(_subdirs DIRECTORY "${_dir}" PROPERTY SUBDIRECTORIES)
    foreach(_subdir IN LISTS _subdirs)
        get_all_targets(${_result} "${_subdir}")
    endforeach()

    get_directory_property(_sub_targets DIRECTORY "${_dir}" BUILDSYSTEM_TARGETS)
    set(${_result} ${${_result}} ${_sub_targets} PARENT_SCOPE)
endfunction()

function(generate-cargo-toml)
    # Sanitize project name for cargo target
    # FIXME: Add more sanitizations?
    set(CARGO_NAME ${PROJECT_NAME})
    string(REGEX REPLACE "-" "_" CARGO_NAME ${CARGO_NAME})

    get_all_targets(ALL_TARGETS ${PROJECT_SOURCE_DIR})
    message(VERBOSE "ALL_TARGETS: ${ALL_TARGETS}")
    # FIXME: This is super error prone because it uses the last target as the desired target
    list(GET ALL_TARGETS -1 PROJECT_TARGET)
    message(VERBOSE "PROJECT_TARGET: ${PROJECT_TARGET}")

    get_target_property(PROJECT_TYPE ${PROJECT_TARGET} TYPE)
    message(VERBOSE "PROJECT_TYPE: ${PROJECT_TYPE}")
    if(${PROJECT_TYPE} STREQUAL "EXECUTABLE")
      set(CARGO_TARGET "[bin]")
      set(CRATE_TYPE "")
    else()
      set(CARGO_TARGET "lib")
      set(CRATE_TYPE "crate-type = [\"lib\", \"cdylib\"]")
    endif()

    get_target_property(TARGET_DIR ${PROJECT_TARGET} SOURCE_DIR)
    message(VERBOSE "TARGET_DIR: ${TARGET_DIR}")
    message(VERBOSE "CMAKE_CURRENT_SOURCE_DIR: ${CMAKE_CURRENT_SOURCE_DIR}")

    get_target_property(PROJECT_SOURCES ${PROJECT_TARGET} SOURCES)
    message(VERBOSE "PROJECT_SOURCES: ${PROJECT_SOURCES}")

    # FIXME: This is super error prone because it uses the first C file as the path for the crate bin
    #        Ideally we'd use the one with the main function in it...
    list(GET PROJECT_SOURCES 0 CARGO_PATH)
    cmake_path(REPLACE_EXTENSION CARGO_PATH rs OUTPUT_VARIABLE CARGO_PATH)
    cmake_path(ABSOLUTE_PATH CARGO_PATH BASE_DIRECTORY "${TARGET_DIR}")
    cmake_path(RELATIVE_PATH CARGO_PATH BASE_DIRECTORY "${CMAKE_CURRENT_SOURCE_DIR}")
    # P01 projects have test_case prefix, so remove it
    if (${CARGO_PATH} MATCHES "test_case/.*")
        message(VERBOSE "Removing test_case from ${CARGO_PATH}!")
        string(REGEX REPLACE "^test_case/" "" CARGO_PATH ${CARGO_PATH})
    endif()

    # Prepend a top-level src/ directory only if the C target does not already
    # have one
    if (NOT(${CARGO_PATH} MATCHES "^src/.*"))
        message(VERBOSE "Prepending src/ to ${CARGO_PATH}!")
        set(CARGO_PATH "src/${CARGO_PATH}")
    endif()
    message(VERBOSE "CARGO_PATH: ${CARGO_PATH}")

    file(GENERATE OUTPUT "Cargo.toml" CONTENT "[package]
name = \"${PROJECT_NAME}\"
version = \"0.1.0\"
edition = \"2024\"

[${CARGO_TARGET}]
name = \"${CARGO_NAME}\"
path = \"${CARGO_PATH}\"
${CRATE_TYPE}

[dev-dependencies]
assert_cmd = \"2.0.17\"
ntest = \"0.9.3\"
predicates = \"3.1.3\"
")

    message(STATUS "Generated Cargo.toml for ${PROJECT_TARGET} target!")
endfunction()

cmake_language(DEFER CALL generate-cargo-toml)
