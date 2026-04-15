# Core sources that are common to both variants --- NO random source here
set(SPHINCS_CORE_COMMON
    src/address.c
    src/fors.c
    src/merkle.c
    src/sign.c
    src/utils.c
    src/utilsx1.c
    src/wots.c
    src/wotsx1.c
)

set(PARAMS "sphincs-${HASH_BACKEND}-${SECPAR}")
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -DPARAMS=${PARAMS}")
if(${HASH_BACKEND} STREQUAL "blake")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -DBLAKE_TR=1")
elseif(${HASH_BACKEND} STREQUAL "haraka")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -DHARAKA_TR=1")
elseif(${HASH_BACKEND} STREQUAL "sha2")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -DSHA2_TR=1")
elseif(${HASH_BACKEND} STREQUAL "shake")
    set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -DSHAKE_TR=1")
endif()

# Build once as an OBJECT library (so tests and apps reuse the same objects)
add_library(sphincs_obj SHARED OBJECT ${SPHINCS_CORE_COMMON})
target_include_directories(sphincs_obj PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

# Normal core (uses randombytes.c)
add_library(sphincs_core SHARED
    $<TARGET_OBJECTS:sphincs_obj>
    src/randombytes.c
)
target_include_directories(sphincs_core PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

# Deterministic core (uses rng.c)
add_library(sphincs_core_det SHARED
    $<TARGET_OBJECTS:sphincs_obj>
    src/rng.c
)
target_include_directories(sphincs_core_det PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)

# Main executable uses the deterministic core (no randombytes.c)
add_executable(driver src/PQCgenKAT_sign.c)

# Link correct hash backend based on parameter
target_link_libraries(driver sphincs_core_det ${HASH_BACKEND} crypto)
