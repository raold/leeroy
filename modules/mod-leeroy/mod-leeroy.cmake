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

    option(LEEROY_TESTS "Build the mod-leeroy unit tests" OFF)
    if (LEEROY_TESTS)
        get_target_property(LEEROY_BOOST_INCLUDE_DIR boost INTERFACE_INCLUDE_DIRECTORIES)
        add_subdirectory("${CMAKE_CURRENT_LIST_DIR}/tests" "${CMAKE_BINARY_DIR}/modules/mod-leeroy-tests")
    endif()
endif()
