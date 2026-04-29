# Phase 5 unit tests (CLEAR — Completion-Labeled Erasure Attribution)
#
# As of W1.1: witness_codec + messages header. More targets will land in
# subsequent W1.x increments (lease_table, control_plane, finalizer).

foreach(test_name test_witness_codec test_lease_table test_control_plane_codec)
    add_executable(${test_name} ${test_name}.cpp)
    target_link_libraries(${test_name}
        PRIVATE clear_transport GTest::gtest_main)
    target_include_directories(${test_name} PRIVATE ${PROJECT_SOURCE_DIR}/src)
    add_test(NAME ${test_name} COMMAND ${test_name})
endforeach()
