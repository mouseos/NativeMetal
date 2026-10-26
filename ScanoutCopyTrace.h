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
    std::atomic<uint64_t> geometry_[5]{}; // width, height, source/target pitch, alignment
    std::atomic<unsigned> route_{3}; // 0 contiguous, 1 separate rows, 2 texture rows, 3 unselected
    std::atomic<bool> returned_{false};
    std::atomic<uint64_t> returnedAt_{0};
    uint64_t (*clock_)() = nullptr;
public:
    struct Snapshot { unsigned phase; bool returned; uint64_t elapsed; uint64_t intervals[4]; uint64_t geometry[5]; unsigned route; };
    // Configure before publishing the server threads; null is the default-off policy.
    void configure(uint64_t (*clock)()) { clock_ = clock; }
    bool begin(uint32_t width=0, uint32_t height=0, uint64_t sourcePitch=0, uint64_t targetPitch=0) {
        if (!clock_ || phase_.load(std::memory_order_relaxed)) return false;
        geometry_[0].store(width, std::memory_order_relaxed);
        geometry_[1].store(height, std::memory_order_relaxed);
        geometry_[2].store(sourcePitch, std::memory_order_relaxed);
        geometry_[3].store(targetPitch, std::memory_order_relaxed);
        times_[0].store(clock_(), std::memory_order_relaxed);
        phase_.store(1, std::memory_order_release); return true;
    }
    void route(unsigned value, uint64_t alignment=0) {
        geometry_[4].store(alignment, std::memory_order_relaxed);
        route_.store(value, std::memory_order_release);
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
        s.route = route_.load(std::memory_order_acquire);
        for (unsigned i=0; i<5; ++i) s.geometry[i] = geometry_[i].load(std::memory_order_relaxed);
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
