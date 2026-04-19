# src/bindings/CMakeLists.txt — Phase 3 Stage A pybind11 extension
#
# Entered from top-level CMakeLists.txt only when -DSEMIRDMA_BUILD_BINDINGS=ON
# (default OFF).  The parent already ran find_package(pybind11 CONFIG) and
# ensured semirdma_transport has POSITION_INDEPENDENT_CODE enabled.

pybind11_add_module(_semirdma_ext MODULE
    py_semirdma.cpp
)

target_link_libraries(_semirdma_ext PRIVATE
    semirdma_transport
)

# setup.py (invoked by pip install -e .) places the compiled .so next to
# python/semirdma/ via a cmake --install step.  For developer workflow
# (raw cmake builds) the output lives under build/src/bindings/.
set_target_properties(_semirdma_ext PROPERTIES
    LIBRARY_OUTPUT_DIRECTORY "${CMAKE_BINARY_DIR}/python/semirdma"
)
