# Phase 2 test binaries
#
# 3 gtest binaries (unit/integration tests)
# 1 non-gtest binary (RQ1 chunk sweep experiment)

# GTest-based tests
foreach(test_name test_chunk_roundtrip test_ratio_timeout test_ghost_mask)
    add_executable(${test_name} ${test_name}.cpp)
    target_link_libraries(${test_name} PRIVATE semirdma_transport GTest::gtest_main)
    add_test(NAME ${test_name} COMMAND ${test_name})
endforeach()

# RQ1 sweep experiment (non-gtest, has its own main)
add_executable(test_chunk_sweep test_chunk_sweep.cpp)
target_link_libraries(test_chunk_sweep PRIVATE semirdma_transport)
