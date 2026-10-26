// SPDX-License-Identifier: MIT
#pragma once
#include <atomic>
#include <stdint.h>
namespace gpupresent {
// One writer (scanout thread), concurrent status reader. Timestamps are microseconds.
// No allocation, BO access, formatting, or output on the copy path.
class CopyTrace {
    std::atomic<unsigned> phase_{0};
    std::atomic<uint64_t> times_[5]{};
    std::atomic<bool> returned_{false};
    std::atomic<uint64_t> returnedAt_{0};
    uint64_t (*clock_)() = nullptr;
public:
    struct Snapshot { unsigned phase; bool returned; uint64_t elapsed; uint64_t intervals[4]; };
    // Configure before publishing the server threads; null is the default-off policy.
    void configure(uint64_t (*clock)()) { clock_ = clock; }
    bool begin() {
        if (!clock_ || phase_.load(std::memory_order_relaxed)) return false;
        times_[0].store(clock_(), std::memory_order_relaxed);
        phase_.store(1, std::memory_order_release); return true;
    }
    void reached(unsigned phase) {
        if (phase < 2 || phase > 5) return;
        times_[phase-1].store(clock_(), std::memory_order_relaxed);
        phase_.store(phase, std::memory_order_release);
    }
    void returned() {
        returnedAt_.store(clock_(), std::memory_order_relaxed);
        returned_.store(true, std::memory_order_release);
    }
    Snapshot snapshot(uint64_t now) const {
        Snapshot s{}; s.returned = returned_.load(std::memory_order_acquire);
        s.phase = phase_.load(std::memory_order_acquire);
        if (!s.phase) return s;
        uint64_t previous = times_[0].load(std::memory_order_relaxed);
        const uint64_t start = previous;
        for (unsigned i=1; i<s.phase; ++i) {
            uint64_t end = times_[i].load(std::memory_order_relaxed);
            s.intervals[i-1] = end-previous; previous=end;
        }
        const uint64_t end = s.phase == 5 ? previous :
            (s.returned ? returnedAt_.load(std::memory_order_relaxed) : now);
        s.elapsed = end >= start ? end-start : 0;
        return s;
    }
};
} // namespace gpupresent
