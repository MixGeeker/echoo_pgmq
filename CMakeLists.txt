cmake_minimum_required(VERSION 3.20)
project(echoo_pgmq VERSION 0.1.2 LANGUAGES C)

option(ECHOO_ENABLE_TEST_HOOKS "Compile administrator-only deterministic fault-injection hooks; NEVER ship this build" OFF)

set(CMAKE_C_STANDARD 11)
set(CMAKE_C_STANDARD_REQUIRED ON)
set(PG_CONFIG "pg_config" CACHE FILEPATH "PostgreSQL 16, 17 or 18 pg_config executable")
set(PROTON_ROOT "" CACHE PATH "Qpid Proton 0.40 installation (OpenSSL backend required)")
set(POSTGRES_LIBRARY "" CACHE FILEPATH "Windows PostgreSQL postgres.lib import library")

function(pg_config_value option output)
  execute_process(COMMAND "${PG_CONFIG}" "${option}"
    OUTPUT_VARIABLE value OUTPUT_STRIP_TRAILING_WHITESPACE
    RESULT_VARIABLE result)
  if(NOT result EQUAL 0)
    message(FATAL_ERROR "${PG_CONFIG} ${option} failed; set -DPG_CONFIG to a PostgreSQL installation")
  endif()
  set(${output} "${value}" PARENT_SCOPE)
endfunction()
pg_config_value(--version PG_VERSION_STRING)
if(NOT PG_VERSION_STRING MATCHES "PostgreSQL (16|17|18)\\.")
  message(FATAL_ERROR "Supported PostgreSQL major versions: 16, 17 and 18; found ${PG_VERSION_STRING}")
endif()
pg_config_value(--includedir-server PG_INCLUDEDIR_SERVER)
pg_config_value(--includedir PG_INCLUDEDIR)
pg_config_value(--pkglibdir PG_PKGLIBDIR)
pg_config_value(--sharedir PG_SHAREDIR)
pg_config_value(--libdir PG_LIBDIR)

find_path(PROTON_INCLUDE_DIR proton/connection_driver.h
  HINTS "${PROTON_ROOT}/include" REQUIRED)
find_library(PROTON_LIBRARY NAMES qpid-proton libqpid-proton
  HINTS "${PROTON_ROOT}/lib" "${PROTON_ROOT}/lib64" REQUIRED)

add_library(echoo_pgmq MODULE src/echoo_pgmq.c src/storage.c src/message.c src/message_validate.c)
set_target_properties(echoo_pgmq PROPERTIES PREFIX "" OUTPUT_NAME "echoo_pgmq"
  BUILD_RPATH "${PROTON_ROOT}/lib;${PROTON_ROOT}/lib64"
  INSTALL_RPATH "$ORIGIN")
target_include_directories(echoo_pgmq PRIVATE include "${PG_INCLUDEDIR_SERVER}" "${PG_INCLUDEDIR}" "${PROTON_INCLUDE_DIR}")
target_link_libraries(echoo_pgmq PRIVATE "${PROTON_LIBRARY}")
if(ECHOO_ENABLE_TEST_HOOKS)
  target_compile_definitions(echoo_pgmq PRIVATE ECHOO_ENABLE_TEST_HOOKS=1)
  message(WARNING "TEST-ONLY fault injection is enabled: do not distribute this module")
endif()
if(WIN32)
  target_include_directories(echoo_pgmq PRIVATE "${PG_INCLUDEDIR_SERVER}/port/win32" "${PG_INCLUDEDIR_SERVER}/port/win32_msvc")
  target_compile_definitions(echoo_pgmq PRIVATE WIN32 _WINDOWS _CRT_SECURE_NO_WARNINGS)
  if(NOT POSTGRES_LIBRARY)
    find_library(PG_SERVER_IMPORT NAMES postgres HINTS "${PG_LIBDIR}" "${PG_PKGLIBDIR}" REQUIRED)
    set(POSTGRES_LIBRARY "${PG_SERVER_IMPORT}")
  endif()
  target_link_libraries(echoo_pgmq PRIVATE "${POSTGRES_LIBRARY}" ws2_32)
else()
  target_compile_options(echoo_pgmq PRIVATE -Wall -Wextra -Wformat=2 -Werror=implicit-function-declaration)
  if(APPLE)
    target_link_options(echoo_pgmq PRIVATE "-undefined" "dynamic_lookup")
    set_target_properties(echoo_pgmq PROPERTIES SUFFIX ".so")
  endif()
endif()

# The caller can override these for packaging/staging. By default CMake installs
# into the pg_config installation, not an unrelated generic /usr/local prefix.
set(ECHOO_EXTENSION_LIBDIR "${PG_PKGLIBDIR}" CACHE PATH "Install directory for PostgreSQL extension modules")
set(ECHOO_EXTENSION_SHAREDIR "${PG_SHAREDIR}/extension" CACHE PATH "Install directory for extension SQL/control")
install(TARGETS echoo_pgmq LIBRARY DESTINATION "${ECHOO_EXTENSION_LIBDIR}" RUNTIME DESTINATION "${ECHOO_EXTENSION_LIBDIR}")
file(GLOB ECHOO_SQL_SCRIPTS CONFIGURE_DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/sql/echoo_pgmq--*.sql")
install(FILES echoo_pgmq.control ${ECHOO_SQL_SCRIPTS} DESTINATION "${ECHOO_EXTENSION_SHAREDIR}")
