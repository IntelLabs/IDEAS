set(SHA2_SOURCES
    src/sha2.c
    src/hash_sha2.c
    ../../app/src/utils.c
    src/thash_sha2_${THASH}.c
)

set(PARAMS "sphincs-sha2-${SECPAR}")
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -DPARAMS=${PARAMS}")

add_library(sha2 SHARED ${SHA2_SOURCES})
target_include_directories(sha2 PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
