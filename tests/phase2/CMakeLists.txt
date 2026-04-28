# Phase 2 test binaries
#
# 3 gtest binaries (unit/integration tests)
# 3 non-gtest binaries (RQ1 chunk sweep, RQ2 RMS error, RQ4 ratio-timeout sweep)

# GTest-based tests
foreach(test_name test_chunk_roundtrip test_ratio_timeout test_ghost_mask
                  test_bucket_id_isolation)
    add_executable(${test_name} ${test_name}.cpp)
    target_link_libraries(${test_name} PRIVATE semirdma_transport GTest::gtest_main)
    add_test(NAME ${test_name} COMMAND ${test_name})
endforeach()

# RQ1 sweep experiment (non-gtest, has its own main)
add_executable(test_chunk_sweep test_chunk_sweep.cpp)
target_link_libraries(test_chunk_sweep PRIVATE semirdma_transport)

# RQ2 RMS error experiment (non-gtest, has its own main)
add_executable(test_rms_error test_rms_error.cpp)
target_link_libraries(test_rms_error PRIVATE semirdma_transport)

# RQ4 ratio/timeout sweep experiment (non-gtest, has its own main)
add_executable(test_ratio_sweep test_ratio_sweep.cpp)
target_link_libraries(test_ratio_sweep PRIVATE semirdma_transport)
