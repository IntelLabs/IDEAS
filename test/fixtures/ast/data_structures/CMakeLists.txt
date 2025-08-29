cmake_minimum_required(VERSION 3.10)
cmake_path(GET CMAKE_CURRENT_SOURCE_DIR PARENT_PATH parent)
cmake_path(GET parent FILENAME project_name)
project(${project_name})
add_library(${project_name}
    src/main.c)

target_include_directories(${project_name} PUBLIC include)
target_include_directories(${project_name} PRIVATE src)

install(TARGETS ${project_name}
    DESTINATION lib)
install(DIRECTORY include/
    DESTINATION include)
