// SPDX-License-Identifier: MIT
#pragma once
#include <atomic>
#include <stdint.h>
namespace gpupresent {
// One writer: the single scanout thread serializes all present calls.
// The status thread reads concurrently. Timestamps are microseconds.
// No allocation, BO access, formatting, or output on the copy path.
class CopyTrace {
    std::atomic<unsigned> phase_{0};
    std::atomic<uint64_t> times_[5]{};
    std::atomic<uint64_t> encodeTimes_[5]{};
    std::atomic<unsigned> encodePhase_{0};
    std::atomic<uint64_t> geometry_[5]{}; // width, height, source/target pitch, alignment
    std::atomic<unsigned> route_{3}; // 0 contiguous, 1 separate rows, 2 texture rows, 3 unselected
    std::atomic<bool> returned_{false};
    std::atomic<uint64_t> returnedAt_{0};
    uint64_t (*clock_)() = nullptr;
    std::atomic<uint64_t> encoderTimes_[25]{};
    std::atomic<uint32_t> encoderMask_{0};
    using EncoderCallback = void (*)(void *, uint32_t);
    void (*encoderSetter_)(EncoderCallback, void *) = nullptr;
    static void encoderCallback(void *context, uint32_t event) {
        auto *trace = static_cast<CopyTrace *>(context);
        if (!trace || !trace->clock_ || event < 1 || event > 25) return;
        const uint32_t bit = 1u << (event-1);
        if (trace->encoderMask_.load(std::memory_order_relaxed) & bit) return;
        trace->encoderTimes_[event-1].store(trace->clock_(), std::memory_order_relaxed);
        trace->encoderMask_.fetch_or(bit, std::memory_order_release);
    }
public:
    struct Snapshot { unsigned phase; bool returned; uint64_t elapsed; uint64_t intervals[4]; uint64_t geometry[5]; unsigned route; unsigned encodePhase; uint64_t encoding[5]; uint32_t encoderMask; uint64_t encoderTimes[25]; };
    // Configure before publishing the server threads; null is the default-off policy.
    void configure(uint64_t (*clock)()) { clock_ = clock; }
    void configureEncoder(void (*setter)(EncoderCallback, void *)) { encoderSetter_ = setter; }
    void encoderEnter() {
        if (clock_ && encoderSetter_) { encoderCallback(this, 24); encoderSetter_(encoderCallback, this); }
    }
    void encoderLeave() {
        if (clock_ && encoderSetter_) { encoderSetter_(nullptr, nullptr); encoderCallback(this, 25); }
    }
    // UINT32_MAX means not started or invalid clock; a started unfinished
    // interval uses the reader's clock (or the scope-end clock after return).
    static uint32_t encoderIntervalUs(const Snapshot &s, unsigned first, unsigned last, uint64_t now) {
        if (first < 1 || first > 25 || last < 1 || last > 25 || !(s.encoderMask & (1u << (first-1)))) return UINT32_MAX;
        const uint64_t start = s.encoderTimes[first-1];
        uint64_t end = now;
        if (s.encoderMask & (1u << (last-1))) end = s.encoderTimes[last-1];
        else if (s.encoderMask & (1u << 24)) end = s.encoderTimes[24];
        if (end < start) return UINT32_MAX;
        const uint64_t elapsed = end - start;
        return elapsed >= UINT32_MAX ? UINT32_MAX-1 : (uint32_t)elapsed;
    }
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
    // q=command buffer, b=blit encoder, t=route/texture setup, x=copy recording, e=endEncoding.
    // t/x depend on the route: interpret with CG/CS. First-copy writer only.
    // No locks, allocation, BO reads, formatting or output.
    void encoded(unsigned step) {
        if (!clock_ || step < 1 || step > 5 || phase_.load(std::memory_order_relaxed) != 2 ||
            step <= encodePhase_.load(std::memory_order_relaxed)) return;
        encodeTimes_[step-1].store(clock_(), std::memory_order_relaxed);
        encodePhase_.store(step, std::memory_order_release);
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
        s.encodePhase = s.phase >= 2 ? encodePhase_.load(std::memory_order_acquire) : 0;
        uint64_t encodePrevious = times_[1].load(std::memory_order_relaxed);
        for (unsigned i=0; i<s.encodePhase; ++i) {
            const uint64_t next = encodeTimes_[i].load(std::memory_order_relaxed);
            s.encoding[i] = next >= encodePrevious ? next-encodePrevious : 0;
            encodePrevious = next;
        }
        s.encoderMask = encoderMask_.load(std::memory_order_acquire);
        for (unsigned i=0; i<25; ++i) if (s.encoderMask & (1u << i))
            s.encoderTimes[i] = encoderTimes_[i].load(std::memory_order_relaxed);
        const uint64_t end = s.phase == 5 ? previous :
            (s.returned ? returnedAt_.load(std::memory_order_relaxed) : now);
        s.elapsed = end >= start ? end-start : 0;
        return s;
    }
};
} // namespace gpupresent
