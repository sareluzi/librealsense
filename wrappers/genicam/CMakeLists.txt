# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.

cmake_minimum_required(VERSION 3.10)

project(RealsenseGenTLProducer)

add_library(realsense2-gentl SHARED
    src/gentl-api.cpp
    src/system.cpp
    src/device.cpp
    src/data-stream.cpp
    src/node-map.cpp
    src/gentl-common.h
    src/modules.h
    src/node-map.h
)

target_include_directories(realsense2-gentl PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/../../third-party/gentl)
# GCTLIDLL makes GenTL.h declare the GenTL functions as exported
target_compile_definitions(realsense2-gentl PRIVATE GCTLIDLL)
target_compile_features(realsense2-gentl PRIVATE cxx_std_14)
target_link_libraries(realsense2-gentl PRIVATE ${DEPENDENCIES})

# GenTL consumers look for *.cti files in GENICAM_GENTL64_PATH; only the GenTL C functions are exported
set_target_properties(realsense2-gentl PROPERTIES
    PREFIX ""
    SUFFIX ".cti"
    CXX_VISIBILITY_PRESET hidden
    VISIBILITY_INLINES_HIDDEN ON
    FOLDER Wrappers
)
if(UNIX AND NOT APPLE)
    # Keep a statically linked librealsense from leaking its symbols into the consumer process
    set_property(TARGET realsense2-gentl APPEND_STRING PROPERTY LINK_FLAGS " -Wl,--exclude-libs,ALL")
endif()

install(TARGETS realsense2-gentl
    LIBRARY DESTINATION ${CMAKE_INSTALL_LIBDIR}/realsense2-gentl
    RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR}
)
