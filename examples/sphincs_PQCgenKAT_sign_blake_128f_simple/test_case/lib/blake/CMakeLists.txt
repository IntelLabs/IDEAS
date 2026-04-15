set(BLAKE_SOURCES
    src/blake256.c
    src/blake512.c
    src/hash_blake.c
    ../../app/src/utils.c
    src/thash_blake_${THASH}.c
)

set(PARAMS "sphincs-blake-${SECPAR}")
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -DPARAMS=${PARAMS}")

add_library(blake SHARED ${BLAKE_SOURCES})
target_include_directories(blake PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
