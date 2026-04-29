# Phase 5 unit tests (CLEAR — Completion-Labeled Erasure Attribution)
#
# As of W1.1: witness_codec + messages header. More targets will land in
# subsequent W1.x increments (lease_table, control_plane, finalizer).

foreach(test_name test_witness_codec test_lease_table test_control_plane_codec
                  test_imm_codec)
    add_executable(${test_name} ${test_name}.cpp)
    target_link_libraries(${test_name}
        PRIVATE clear_transport GTest::gtest_main)
    target_include_directories(${test_name} PRIVATE ${PROJECT_SOURCE_DIR}/src)
    add_test(NAME ${test_name} COMMAND ${test_name})
endforeach()

# RDMA-gated loopback test. Skips itself at runtime if RDMA_LOOPBACK_DEVICE
# is not set, so it's safe to include unconditionally in ctest.
add_executable(test_control_plane_loopback test_control_plane_loopback.cpp)
target_link_libraries(test_control_plane_loopback
    PRIVATE clear_transport GTest::gtest_main)
target_include_directories(test_control_plane_loopback
    PRIVATE ${PROJECT_SOURCE_DIR}/src)
add_test(NAME test_control_plane_loopback COMMAND test_control_plane_loopback)

# RDMA-gated end-to-end test for RatioController CLEAR mode.
# Skips at runtime if RDMA_LOOPBACK_DEVICE is not set.
add_executable(test_ratio_clear_loopback test_ratio_clear_loopback.cpp)
target_link_libraries(test_ratio_clear_loopback
    PRIVATE clear_transport GTest::gtest_main)
target_include_directories(test_ratio_clear_loopback
    PRIVATE ${PROJECT_SOURCE_DIR}/src)
add_test(NAME test_ratio_clear_loopback COMMAND test_ratio_clear_loopback)
