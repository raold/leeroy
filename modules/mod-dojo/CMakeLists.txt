# This file is part of leeroy (mod-dojo), released under the GNU GPL v2. See LICENSE.

# mod-dojo: registers src/ with the AzerothCore script loader. Runs from
# add_subdirectory before the `modules` target exists, so it must not touch
# targets; target settings live in mod-dojo.cmake. tests/ is a standalone
# project of its own and never part of a core build.
AC_ADD_SCRIPT("${CMAKE_CURRENT_LIST_DIR}/src")
