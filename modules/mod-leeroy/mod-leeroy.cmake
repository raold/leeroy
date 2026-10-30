# This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

# Included from modules/CMakeLists.txt after the `modules` target exists.
if (TARGET modules)
    # SYSTEM keeps the vendored json.hpp out of -Werror.
    target_include_directories(modules SYSTEM PRIVATE "${CMAKE_CURRENT_LIST_DIR}/deps")

    if (MSVC)
        # The trailing "=" matters, see mod-dungeon-clear.cmake.
        get_target_property(LEEROY_MODULES_OPTIONS modules COMPILE_OPTIONS)
        if (NOT LEEROY_MODULES_OPTIONS MATCHES "_USE_MATH_DEFINES")
            target_compile_options(modules PRIVATE /D_USE_MATH_DEFINES=)
        endif()
    endif()

    # Integration probes, by token in the other modules' headers (like mod-dungeon-clear's core probes). The pin
    # API is required. mod-ollama-chat without the speaker filter would give every cast member two voices, so a
    # built but unpatched mod-ollama-chat is an error too. mod-dungeon-clear is optional.
    function(leeroy_module_active name out)
        string(TOUPPER "${name}" upper)
        if (EXISTS "${CMAKE_SOURCE_DIR}/modules/${name}"
            AND NOT ";${DISABLED_AC_MODULES};" MATCHES ";${name};"
            AND NOT "${MODULE_${upper}}" STREQUAL "disabled")
            set(${out} TRUE PARENT_SCOPE)
        else()
            set(${out} FALSE PARENT_SCOPE)
        endif()
    endfunction()

    set(LEEROY_PLAYERBOTS_H "${CMAKE_SOURCE_DIR}/modules/mod-playerbots/src/Bot/RandomPlayerbotMgr.h")
    set(LEEROY_PROBE_TEXT "")
    if (EXISTS "${LEEROY_PLAYERBOTS_H}")
        file(READ "${LEEROY_PLAYERBOTS_H}" LEEROY_PROBE_TEXT)
    endif()
    string(FIND "${LEEROY_PROBE_TEXT}" "SetPinnedBots" LEEROY_PROBE_POS)
    if (LEEROY_PROBE_POS EQUAL -1)
        message(FATAL_ERROR "[mod-leeroy] mod-playerbots has no pin API: apply leeroy patches/mod-playerbots")
    endif()

    set(LEEROY_HAS_OLLAMA_FILTER 0)
    set(LEEROY_OLLAMA_H "${CMAKE_SOURCE_DIR}/modules/mod-ollama-chat/src/mod-ollama-chat_dispatch.h")
    leeroy_module_active(mod-ollama-chat LEEROY_OLLAMA_ACTIVE)
    if (LEEROY_OLLAMA_ACTIVE AND EXISTS "${LEEROY_OLLAMA_H}")
        file(READ "${LEEROY_OLLAMA_H}" LEEROY_PROBE_TEXT)
        string(FIND "${LEEROY_PROBE_TEXT}" "OllamaDispatch_SetSpeakerFilter" LEEROY_PROBE_POS)
        if (LEEROY_PROBE_POS EQUAL -1)
            message(FATAL_ERROR "[mod-leeroy] mod-ollama-chat has no speaker filter, so the cast would speak twice: "
                "apply leeroy patches/mod-ollama-chat")
        endif()
        set(LEEROY_HAS_OLLAMA_FILTER 1)
    endif()

    set(LEEROY_HAS_DUNGEON_CLEAR 0)
    set(LEEROY_DC_SRC "${CMAKE_SOURCE_DIR}/modules/mod-dungeon-clear/src")
    set(LEEROY_DC_PUBLISHER_H "${LEEROY_DC_SRC}/Ai/Dungeon/DungeonClear/Util/DcStatusPublisher.h")
    leeroy_module_active(mod-dungeon-clear LEEROY_DC_ACTIVE)
    if (LEEROY_DC_ACTIVE AND EXISTS "${LEEROY_DC_PUBLISHER_H}"
        AND EXISTS "${LEEROY_DC_SRC}/TestRun/DcTestRunManager.h"
        AND EXISTS "${LEEROY_DC_SRC}/Ai/Dungeon/DungeonClear/Util/DcRun.h")
        file(READ "${LEEROY_DC_PUBLISHER_H}" LEEROY_PROBE_TEXT)
        string(FIND "${LEEROY_PROBE_TEXT}" "SetStatusObserver" LEEROY_PROBE_POS)
        if (NOT LEEROY_PROBE_POS EQUAL -1)
            set(LEEROY_HAS_DUNGEON_CLEAR 1)
        endif()
    endif()

    target_compile_definitions(modules PRIVATE
        LEEROY_HAS_OLLAMA_FILTER=${LEEROY_HAS_OLLAMA_FILTER}
        LEEROY_HAS_DUNGEON_CLEAR=${LEEROY_HAS_DUNGEON_CLEAR})
    set(LEEROY_OLLAMA_STATE OFF)
    if (LEEROY_HAS_OLLAMA_FILTER)
        set(LEEROY_OLLAMA_STATE ON)
    endif()
    set(LEEROY_DC_STATE OFF)
    if (LEEROY_HAS_DUNGEON_CLEAR)
        set(LEEROY_DC_STATE ON)
    endif()
    message(STATUS "[mod-leeroy] ollama filter: ${LEEROY_OLLAMA_STATE}, dungeon-clear: ${LEEROY_DC_STATE}")

    option(LEEROY_TESTS "Build the mod-leeroy unit tests" OFF)
    if (LEEROY_TESTS)
        get_target_property(LEEROY_BOOST_INCLUDE_DIR boost INTERFACE_INCLUDE_DIRECTORIES)
        add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/tests" "${CMAKE_BINARY_DIR}/modules/mod-leeroy-tests")
    endif()
endif()
