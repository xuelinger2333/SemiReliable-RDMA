cmake_minimum_required(VERSION 3.16)
project(SemiRDMA CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Dependencies
find_library(IBVERBS_LIB ibverbs REQUIRED)
find_package(Threads REQUIRED)

# Core transport library
add_library(semirdma_transport STATIC
    src/transport/uc_qp_engine.cpp
    src/transport/chunk_manager.cpp
    src/transport/ratio_controller.cpp
    src/transport/ghost_mask.cpp
)
target_include_directories(semirdma_transport PUBLIC src/)
target_link_libraries(semirdma_transport PUBLIC ${IBVERBS_LIB} Threads::Threads)

# Phase 5 CLEAR transport — additive library; legacy semirdma_transport remains
# linkable on its own. Built unconditionally; CLEAR code is reachable only when
# Python config sets transport.mode == "clear", so this is zero-cost for
# existing flat / layer_aware paths.
add_library(clear_transport STATIC
    src/transport/clear/witness_codec.cpp
)
target_include_directories(clear_transport PUBLIC src/)
target_link_libraries(clear_transport PUBLIC semirdma_transport)

# Phase 2 + Phase 5 tests (GTest optional — main lib builds without it)
find_package(GTest QUIET)
if(GTest_FOUND)
    enable_testing()
    add_subdirectory(tests/phase2)
    add_subdirectory(tests/phase5)
else()
    message(STATUS "GTest not found — skipping tests/phase2 and tests/phase5. "
                   "Install: sudo apt install libgtest-dev && "
                   "cd /usr/src/googletest && sudo cmake . && sudo make install")
endif()

# Phase 3 Stage A: optional Python bindings (pybind11)
# Default OFF — Phase 2 build path is unaffected when the option is not passed.
option(SEMIRDMA_BUILD_BINDINGS "Build pybind11 Python bindings (requires pybind11)" OFF)
if(SEMIRDMA_BUILD_BINDINGS)
    # Produce position-independent code so the static transport lib can be
    # linked into the _semirdma_ext shared module.
    set_property(TARGET semirdma_transport PROPERTY POSITION_INDEPENDENT_CODE ON)
    find_package(pybind11 CONFIG QUIET)
    if(NOT pybind11_FOUND)
        message(FATAL_ERROR
            "SEMIRDMA_BUILD_BINDINGS=ON but pybind11 not found. "
            "Install: pip install pybind11>=2.11")
    endif()
    if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/src/bindings/CMakeLists.txt")
        add_subdirectory(src/bindings)
    else()
        message(STATUS "src/bindings/CMakeLists.txt not present yet — "
                       "bindings target will appear in a later Phase 3 commit.")
    endif()
else()
    message(STATUS "Python bindings disabled (pass -DSEMIRDMA_BUILD_BINDINGS=ON to enable).")
endif()
