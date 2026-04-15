set(SHAKE_SOURCES
    src/fips202.c
    src/hash_shake.c
    src/thash_shake_${THASH}.c
)

set(PARAMS "sphincs-shake-${SECPAR}")
set(CMAKE_C_FLAGS "${CMAKE_C_FLAGS} -DPARAMS=${PARAMS}")

add_library(shake SHARED ${SHAKE_SOURCES})
target_include_directories(shake PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/include)
