# This file is part of leeroy (mod-leeroy), released under the GNU GPL v2. See LICENSE.

# mod-leeroy: registers src/ with the AzerothCore script loader. Runs from
# add_subdirectory before the `modules` target exists, so it must not touch
# targets; target settings live in mod-leeroy.cmake.
AC_ADD_SCRIPT("${CMAKE_CURRENT_LIST_DIR}/src")
