# This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

# Included from modules/CMakeLists.txt after the `modules` target exists. include() keeps that directory's scope, so
# the source properties set here reach the `modules` target.
if (TARGET modules)
    # SYSTEM keeps the vendored json.hpp out of -Werror. mod-leeroy puts its own copy on the same target and the first
    # include directory wins, so in a core that has both the two copies must be byte-identical.
    set(DOJO_JSON "${CMAKE_CURRENT_LIST_DIR}/deps/nlohmann/json.hpp")
    set(DOJO_LEEROY_JSON "${CMAKE_SOURCE_DIR}/modules/mod-leeroy/deps/nlohmann/json.hpp")
    if (EXISTS "${DOJO_LEEROY_JSON}")
        file(SHA256 "${DOJO_JSON}" DOJO_JSON_SHA256)
        file(SHA256 "${DOJO_LEEROY_JSON}" DOJO_LEEROY_JSON_SHA256)
        if (NOT DOJO_JSON_SHA256 STREQUAL DOJO_LEEROY_JSON_SHA256)
            message(FATAL_ERROR "[mod-dojo] deps/nlohmann/json.hpp differs from mod-leeroy's copy; both sit on the "
                "modules target's include path, so keep them identical")
        endif()
    endif()
    target_include_directories(modules SYSTEM PRIVATE "${CMAKE_CURRENT_LIST_DIR}/deps")

    # The training variant, off by default: a core that merely contains mod-dojo builds the disabled variant, which
    # refuses Dojo.Enable = 1. The core's time and control hooks will read this same switch; until that patch series
    # declares it in conf/dist/config.cmake (and reports it in src/cmake/showoptions.cmake), it is declared here, and
    # this option() then does nothing.
    option(AC_ENABLE_DOJO "Build the dojo training variant of mod-dojo (never for a live server)" OFF)
    if (AC_ENABLE_DOJO)
        set(DOJO_TRAINING_BUILD 1)
        # The training runtime logs fixture actors in through mod-playerbots, so it needs it in the same core.
        if (NOT ";${MODULES_MODULE_LIST};" MATCHES ";mod-playerbots;")
            message(FATAL_ERROR "[mod-dojo] AC_ENABLE_DOJO needs mod-playerbots in ${CMAKE_SOURCE_DIR}/modules")
        endif()
        # D02: the training variant builds only with the minimal module set, so it never shares a core with the
        # live modules (mod-leeroy's bridge among them).
        foreach(DOJO_MODULE ${MODULES_MODULE_LIST})
            string(TOUPPER "${DOJO_MODULE}" DOJO_MODULE_UPPER)
            if (NOT DOJO_MODULE MATCHES "^(mod-dojo|mod-playerbots)$"
                AND NOT ";${DISABLED_AC_MODULES};" MATCHES ";${DOJO_MODULE};"
                AND NOT "${MODULE_${DOJO_MODULE_UPPER}}" STREQUAL "disabled")
                message(FATAL_ERROR "[mod-dojo] AC_ENABLE_DOJO builds only mod-playerbots and mod-dojo; "
                    "${DOJO_MODULE} is also in ${CMAKE_SOURCE_DIR}/modules")
            endif()
        endforeach()
    else()
        set(DOJO_TRAINING_BUILD 0)
    endif()

    # Only the runtime files see the switch, so flipping it recompiles them alone. They refuse to compile without it,
    # which also catches this file being skipped because the module folder is not named exactly mod-dojo.
    file(GLOB DOJO_RUNTIME_SOURCES "${CMAKE_CURRENT_LIST_DIR}/src/Runtime/*.cpp")
    set_property(SOURCE ${DOJO_RUNTIME_SOURCES} APPEND PROPERTY COMPILE_DEFINITIONS
        DOJO_TRAINING_BUILD=${DOJO_TRAINING_BUILD})

    if (AC_ENABLE_DOJO)
        message(STATUS "[mod-dojo] training variant (AC_ENABLE_DOJO=ON)")
    else()
        message(STATUS "[mod-dojo] disabled variant (AC_ENABLE_DOJO=OFF)")
    endif()
endif()
