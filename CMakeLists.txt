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

# Phase 2 tests (GTest optional — main lib builds without it)
find_package(GTest QUIET)
if(GTest_FOUND)
    enable_testing()
    add_subdirectory(tests/phase2)
else()
    message(STATUS "GTest not found — skipping tests/phase2. "
                   "Install: sudo apt install libgtest-dev && "
                   "cd /usr/src/googletest && sudo cmake . && sudo make install")
endif()
