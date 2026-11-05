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
target_compile_definitions(dojo_tests PRIVATE DOJO_TEST_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/fixtures"
    DOJO_SCENARIOS="${CMAKE_CURRENT_SOURCE_DIR}/../../../dojo/scenarios")

# The transport uses header-only boost.asio, as mod-leeroy's bridge does.
set(DOJO_BOOST_INCLUDE_DIR "" CACHE PATH "Boost include directory (for boost.asio)")
if (DOJO_BOOST_INCLUDE_DIR)
    target_include_directories(dojo_tests SYSTEM PRIVATE "${DOJO_BOOST_INCLUDE_DIR}")
endif()
target_compile_definitions(dojo_tests PRIVATE BOOST_ASIO_NO_DEPRECATED)

find_package(Threads REQUIRED)
target_link_libraries(dojo_tests PRIVATE Threads::Threads)

if (WIN32)
    target_compile_definitions(dojo_tests PRIVATE _WIN32_WINNT=0x0A00)
    target_link_libraries(dojo_tests PRIVATE ws2_32 mswsock)
endif()

option(DOJO_WARNINGS_AS_ERRORS "Fail the build on any compiler warning (CI turns this on)" OFF)
if (MSVC)
    target_compile_options(dojo_tests PRIVATE /utf-8 /W4 /permissive- /EHsc)
    if (DOJO_WARNINGS_AS_ERRORS)
        target_compile_options(dojo_tests PRIVATE /WX)
    endif()
else()
    target_compile_options(dojo_tests PRIVATE -Wall -Wextra)
    if (DOJO_WARNINGS_AS_ERRORS)
        target_compile_options(dojo_tests PRIVATE -Werror)
    endif()
endif()

enable_testing()
add_test(NAME dojo_tests COMMAND dojo_tests)
