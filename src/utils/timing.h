/*
 * timing.h — std::chrono-based stopwatch for SemiRDMA
 *
 * Header-only.  Replaces Phase 1's clock_gettime(CLOCK_MONOTONIC, ...)
 * with a C++ steady_clock wrapper.
 */

#pragma once

#include <chrono>

namespace semirdma {

class Stopwatch {
public:
    Stopwatch() { reset(); }

    void reset() {
        start_ = std::chrono::steady_clock::now();
    }

    double elapsed_ms() const {
        auto now = std::chrono::steady_clock::now();
        return std::chrono::duration<double, std::milli>(now - start_).count();
    }

    double elapsed_us() const {
        auto now = std::chrono::steady_clock::now();
        return std::chrono::duration<double, std::micro>(now - start_).count();
    }

private:
    std::chrono::steady_clock::time_point start_;
};

} // namespace semirdma
