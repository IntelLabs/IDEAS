cmake_minimum_required(VERSION 4.0)

function(generate-cargo-toml)
    get_property(ALL_TARGETS DIRECTORY ${PROJECT_SOURCE_DIR} PROPERTY BUILDSYSTEM_TARGETS)
    # FIXME: This is super error prone because it uses the first target as the desired target
    list(GET ALL_TARGETS 0 PROJECT_TARGET)

    set(CARGO_NAME ${PROJECT_NAME})
    set(PROJECT_TYPE $<TARGET_PROPERTY:${PROJECT_TARGET},TYPE>)
    set(CARGO_TARGET $<IF:$<STREQUAL:${PROJECT_TYPE},EXECUTABLE>,[bin],lib>)

    set(PROJECT_SOURCES $<TARGET_PROPERTY:${PROJECT_TARGET},SOURCES>)
    set(CARGO_PATHS $<PATH:REPLACE_EXTENSION,${PROJECT_SOURCES},.rs>)
    # FIXME: This is super error prone because it uses the first C file as the path for the crate bin
    set(CARGO_PATH $<LIST:GET,${CARGO_PATHS},0>)

    file(GENERATE OUTPUT "Cargo.toml" CONTENT "[package]
name = \"${PROJECT_NAME}\"
version = \"0.1.0\"
edition = \"2024\"

[${CARGO_TARGET}]
name = \"${CARGO_NAME}\"
path = \"${CARGO_PATH}\"

[dev-dependencies]
assert_cmd = \"2.0.17\"
ntest = \"0.9.3\"
predicates = \"3.1.3\"
")

    message(STATUS "Generated Cargo.toml for ${PROJECT_TARGET} target!")
endfunction()

cmake_language(DEFER CALL generate-cargo-toml)
