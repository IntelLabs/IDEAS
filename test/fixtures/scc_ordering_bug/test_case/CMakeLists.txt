cmake_minimum_required(VERSION 3.10)
project(scc_ordering_bug)

# Reproduces SCC ordering bug in consolidation:
#   caller.c (rank 1) defines compute() which calls helper().
#   state.c (rank 2) defines helper() (inline) and vtable = { .fn = compute }.
#   Cycle: compute -> helper -> vtable -> compute.
#   merge_symbols picks helper from state.c (definition beats declaration).
#   Within the SCC, compute (rank 1) is emitted before helper (rank 2).
#   helper has declaration=None (inline) so no forward decl is emitted.
#   Result: "call to undeclared function 'helper'"
add_executable(scc_ordering_bug
    src/main.c
    src/caller.c
    src/state.c
)
target_include_directories(scc_ordering_bug PRIVATE include)
