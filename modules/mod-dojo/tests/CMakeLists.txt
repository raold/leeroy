# This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

# The pure mod-dojo library and its tests: no AzerothCore, no database, no server.
cmake_minimum_required(VERSION 3.16)
project(dojo_tests CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

file(GLOB DOJO_TEST_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/*.cpp")
file(GLOB DOJO_LIB_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/../src/Lib/*.cpp")

add_executable(dojo_tests ${DOJO_TEST_SOURCES} ${DOJO_LIB_SOURCES})

target_include_directories(dojo_tests PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../src/Lib")
target_include_directories(dojo_tests SYSTEM PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/../deps")
target_compile_definitions(dojo_tests PRIVATE DOJO_TEST_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/fixtures")

if (MSVC)
    target_compile_options(dojo_tests PRIVATE /utf-8 /W4 /permissive- /EHsc)
else()
    target_compile_options(dojo_tests PRIVATE -Wall -Wextra)
endif()

enable_testing()
add_test(NAME dojo_tests COMMAND dojo_tests)
