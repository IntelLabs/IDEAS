set(HARAKA_SOURCES
    src/haraka.c
    src/hash_haraka.c
    src/thash_haraka_${THASH}.c
)

set(PARAMS "sphincs-haraka-${SECPAR}")
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -DPARAMS=${PARAMS}")

add_library(haraka SHARED ${HARAKA_SOURCES})
target_include_directories(haraka PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
