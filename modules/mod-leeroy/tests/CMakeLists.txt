# This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

cmake_minimum_required(VERSION 3.16)
project(leeroy_tests CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

file(GLOB LEEROY_TEST_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/*.cpp")
file(GLOB LEEROY_LIB_SOURCES CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/../src/Lib/*.cpp")

add_executable(leeroy_tests ${LEEROY_TEST_SOURCES} ${LEEROY_LIB_SOURCES})

# Compile the real receipt adapter against delayed database/game boundary fakes.
# Only its AzerothCore includes are replaced; changes to the implementation must
# exercise these tests, and synchronous database calls cannot compile in the fake.
foreach(part h cpp)
    set(receipt_source "${CMAKE_CURRENT_SOURCE_DIR}/../src/Guild/MemberDungeonReceipts.${part}")
    set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${receipt_source}")
    file(READ "${receipt_source}" receipt_text)
    string(FIND "${receipt_text}" "namespace Leeroy::Guild" receipt_begin)
    string(SUBSTRING "${receipt_text}" ${receipt_begin} -1 receipt_body)
    file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/MemberDungeonReceipts.${part}.inc" "${receipt_body}")
endforeach()
set(raid_source "${CMAKE_CURRENT_SOURCE_DIR}/../src/Guild/OfflineRaidJob.cpp")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${raid_source}")
file(READ "${raid_source}" raid_text)
string(FIND "${raid_text}" "bool Checkpoint(Job& job" checkpoint_begin)
string(FIND "${raid_text}" "void SetupFailure(Job& job" checkpoint_end)
math(EXPR checkpoint_size "${checkpoint_end}-${checkpoint_begin}")
string(SUBSTRING "${raid_text}" ${checkpoint_begin} ${checkpoint_size} checkpoint_body)
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/MemberDungeonCheckpoint.inc" "${checkpoint_body}")
foreach(part Admission Replay Stop Tick Disable)
    if(part STREQUAL "Admission")
        set(part_start "bool AwaitDungeonPersistence(Job& job)")
        set(part_end "bool Checkpoint(Job& job")
    elseif(part STREQUAL "Replay")
        set(part_start "std::optional<LeeroyCommandResult> MemberDungeonStartReplay(")
        set(part_end "LeeroyCommandResult StartRaidJob(")
    elseif(part STREQUAL "Stop")
        set(part_start "void Stop(Job& job")
        set(part_end "bool AwaitDungeonPersistence(Job& job)")
    elseif(part STREQUAL "Tick")
        set(part_start "void TickOfflineRaid(")
        set(part_end "std::string MemberDungeonFor(")
    else()
        set(part_start "void DisableOwnedRun(Job& job)")
        set(part_end "bool RestoreWaitOverride(Job& job)")
    endif()
    string(FIND "${raid_text}" "${part_start}" part_begin)
    string(FIND "${raid_text}" "${part_end}" part_finish)
    math(EXPR part_size "${part_finish}-${part_begin}")
    string(SUBSTRING "${raid_text}" ${part_begin} ${part_size} part_body)
    file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/MemberDungeon${part}.inc" "${part_body}")
endforeach()
set(command_source "${CMAKE_CURRENT_SOURCE_DIR}/../src/LeeroyCommands.cpp")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${command_source}")
file(READ "${command_source}" command_text)
string(FIND "${command_text}" "std::string ExecuteExtension(" command_begin)
string(FIND "${command_text}" "ObjectGuid PlayerGuid(" command_end)
math(EXPR command_size "${command_end}-${command_begin}")
string(SUBSTRING "${command_text}" ${command_begin} ${command_size} command_body)
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/MemberDungeonDispatch.inc" "${command_body}")

# Exercise the production mail commit/readback lane with delayed database boundaries.
set(mail_source "${CMAKE_CURRENT_SOURCE_DIR}/../src/Realm/RealmMail.cpp")
set_property(DIRECTORY APPEND PROPERTY CMAKE_CONFIGURE_DEPENDS "${mail_source}")
file(READ "${mail_source}" mail_text)
string(FIND "${mail_text}" "void CommitMail(" mail_begin)
string(FIND "${mail_text}" "RealmStep Send(" mail_end)
math(EXPR mail_size "${mail_end}-${mail_begin}")
string(SUBSTRING "${mail_text}" ${mail_begin} ${mail_size} mail_body)
file(WRITE "${CMAKE_CURRENT_BINARY_DIR}/RealmMailCommit.inc" "${mail_body}")

target_include_directories(leeroy_tests PRIVATE
    "${CMAKE_CURRENT_BINARY_DIR}"
    "${CMAKE_CURRENT_SOURCE_DIR}/../src/Lib"
    "${CMAKE_CURRENT_SOURCE_DIR}/../deps")

set(LEEROY_BOOST_INCLUDE_DIR "" CACHE PATH "Boost include directory (for boost.asio)")
if (LEEROY_BOOST_INCLUDE_DIR)
    target_include_directories(leeroy_tests SYSTEM PRIVATE "${LEEROY_BOOST_INCLUDE_DIR}")
endif()

target_compile_definitions(leeroy_tests PRIVATE BOOST_ASIO_NO_DEPRECATED)
target_compile_definitions(leeroy_tests PRIVATE LEEROY_TEST_FIXTURES="${CMAKE_CURRENT_SOURCE_DIR}/fixtures")

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
