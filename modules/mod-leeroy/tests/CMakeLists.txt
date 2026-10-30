# This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

cmake_minimum_required(VERSION 3.16)
project(leeroy_tests CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

file(GLOB LEEROY_TEST_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/*.cpp")
file(GLOB LEEROY_LIB_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/../src/Lib/*.cpp")

add_executable(leeroy_tests ${LEEROY_TEST_SOURCES} ${LEEROY_LIB_SOURCES})

target_include_directories(leeroy_tests PRIVATE
    "${CMAKE_CURRENT_SOURCE_DIR}/../src/Lib"
    "${CMAKE_CURRENT_SOURCE_DIR}/../deps")

set(LEEROY_BOOST_INCLUDE_DIR "" CACHE PATH "Boost include directory (for boost.asio)")
if (LEEROY_BOOST_INCLUDE_DIR)
    target_include_directories(leeroy_tests SYSTEM PRIVATE "${LEEROY_BOOST_INCLUDE_DIR}")
endif()

target_compile_definitions(leeroy_tests PRIVATE BOOST_ASIO_NO_DEPRECATED)

find_package(Threads REQUIRED)
target_link_libraries(leeroy_tests PRIVATE Threads::Threads)

if (WIN32)
    target_compile_definitions(leeroy_tests PRIVATE _WIN32_WINNT=0x0A00)
    target_link_libraries(leeroy_tests PRIVATE ws2_32 mswsock)
endif()

if (MSVC)
    target_compile_options(leeroy_tests PRIVATE /utf-8 /W4 /permissive- /EHsc)
else()
    target_compile_options(leeroy_tests PRIVATE -Wall -Wextra)
endif()

enable_testing()
add_test(NAME leeroy_tests COMMAND leeroy_tests)
