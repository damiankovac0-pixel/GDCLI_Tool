cmake_minimum_required(VERSION 3.25)

project(GDCLIBridge VERSION 0.1.0 LANGUAGES CXX)

set(GEODE_SDK "" CACHE PATH "Path to the pinned Geode SDK checkout")
if(NOT GEODE_SDK AND DEFINED ENV{GEODE_SDK})
    set(GEODE_SDK "$ENV{GEODE_SDK}")
endif()
if(NOT GEODE_SDK)
    message(FATAL_ERROR "Set GEODE_SDK to the pinned Geode SDK checkout")
endif()
if(NOT IS_DIRECTORY "${GEODE_SDK}")
    message(FATAL_ERROR "GEODE_SDK is not a directory: ${GEODE_SDK}")
endif()

add_subdirectory("${GEODE_SDK}" "${CMAKE_CURRENT_BINARY_DIR}/geode-sdk")

add_library(${PROJECT_NAME} SHARED src/main.cpp)
target_compile_features(${PROJECT_NAME} PRIVATE cxx_std_23)
set_target_properties(${PROJECT_NAME} PROPERTIES PREFIX "")

setup_geode_mod(${PROJECT_NAME} DONT_INSTALL)
