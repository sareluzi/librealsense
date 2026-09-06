# License: Apache 2.0. See LICENSE file in root directory.
# Copyright(c) 2026 RealSense, Inc. All Rights Reserved.
cmake_minimum_required(VERSION 3.10)

project(RealsenseExamplesTemporalFilterDppMock)

# Test scaffolding proving the atomicity + round-trip behavior of the generic composite-option
# mechanism against a fake in-memory composite_option_interface standing in for real HKR/D555
# hardware, driven through the real public entry points - see rs-temporal-filter-dpp-mock.cpp.
add_executable(rs-temporal-filter-dpp-mock
    rs-temporal-filter-dpp-mock.cpp
)
set_property(TARGET rs-temporal-filter-dpp-mock PROPERTY CXX_STANDARD 14)
target_link_libraries(rs-temporal-filter-dpp-mock PRIVATE ${LRS_TARGET})
set_target_properties (rs-temporal-filter-dpp-mock PROPERTIES FOLDER Examples)
install(TARGETS rs-temporal-filter-dpp-mock RUNTIME DESTINATION ${CMAKE_INSTALL_BINDIR})
