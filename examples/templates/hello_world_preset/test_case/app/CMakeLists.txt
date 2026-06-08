add_executable(hello_world src/main.c)
target_link_libraries(hello_world PRIVATE hello_world_lib)
