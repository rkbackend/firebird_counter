cmake_minimum_required(VERSION 3.20)
project(firebird_procedure_counter LANGUAGES CXX)

option(PROC_USAGE_ENABLE_FIREBIRD_SDK "Build the Firebird trace plugin bridge" OFF)
set(FIREBIRD_INCLUDE_DIR "" CACHE PATH "Path to Firebird SDK headers")

add_library(proc_usage_core
    cpp/src/config.cpp
    cpp/src/collector.cpp
    cpp/src/spool_writer.cpp
    cpp/src/firebird_bridge.cpp
)

target_include_directories(proc_usage_core
    PUBLIC
        ${CMAKE_CURRENT_SOURCE_DIR}/cpp/include
)

target_compile_features(proc_usage_core PUBLIC cxx_std_20)
set_target_properties(proc_usage_core PROPERTIES POSITION_INDEPENDENT_CODE ON)

if(PROC_USAGE_ENABLE_FIREBIRD_SDK)
    if(NOT FIREBIRD_INCLUDE_DIR)
        message(FATAL_ERROR "FIREBIRD_INCLUDE_DIR must point at the Firebird SDK headers when PROC_USAGE_ENABLE_FIREBIRD_SDK=ON")
    endif()

    add_library(proc_usage_firebird_plugin SHARED
        cpp/src/plugin_entry.cpp
    )

    target_include_directories(proc_usage_firebird_plugin
        PRIVATE
            ${CMAKE_CURRENT_SOURCE_DIR}/cpp/include
            ${FIREBIRD_INCLUDE_DIR}
    )

    target_compile_definitions(proc_usage_firebird_plugin
        PRIVATE
            PROC_USAGE_ENABLE_FIREBIRD_SDK=1
    )

    target_compile_features(proc_usage_firebird_plugin PRIVATE cxx_std_20)
    target_link_libraries(proc_usage_firebird_plugin PRIVATE proc_usage_core)
    set_target_properties(proc_usage_firebird_plugin PROPERTIES OUTPUT_NAME "proc_usage_trace")
endif()
