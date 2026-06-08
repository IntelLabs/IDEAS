add_library(hello_world_lib SHARED src/hello.c)
target_include_directories(hello_world_lib PUBLIC include)
