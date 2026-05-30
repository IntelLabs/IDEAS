cmake_minimum_required(VERSION 3.10)
project(typedef_cross_tu)

# Library with b.c and c.c — these get parsed from compile_commands.json
# before a.c (because library objects are built first). merge_symbols
# processes b.c first → retains typedef X cursor from b.c.
add_library(xlib SHARED src/b.c src/c.c)
target_include_directories(xlib PRIVATE include)

# Executable links against xlib; its .sources lists a.c first (from
# add_executable sources) then lib sources appended via LINK_LIBRARIES.
# So source_priority = [a.c, b.c, c.c] → ast_order rank: a.c=0, b.c=1, c.c=2
# This gives struct X (from a.c) rank 0 < typedef X (from b.c) rank 1.
add_executable(typedef_cross_tu src/a.c)
target_include_directories(typedef_cross_tu PRIVATE include)
target_link_libraries(typedef_cross_tu xlib)
