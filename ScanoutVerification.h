// SPDX-License-Identifier: MIT
#pragma once
#include <atomic>
#include <stdint.h>
#include <stddef.h>
namespace gpupresent {
// One worker, one service thread. Pixels live in a separately retained Metal buffer.
struct VerificationResult {
    std::atomic<bool> busy{false}, ready{false};
    uint64_t mismatches = 0;
    static uint64_t compare(const uint32_t *snapshot, uint32_t width, uint32_t rows) {
        uint64_t mismatches = 0;
        const uint32_t *shown = snapshot + (size_t)width * rows;
        for (uint32_t y = 0; y < rows; ++y)
            for (uint32_t x = 0; x < width; ++x)
                if (snapshot[(size_t)y * width + x] != shown[(size_t)y * width + x]) { ++mismatches; break; }
        return mismatches;
    }
    void publish(uint64_t value) {
        mismatches = value;
        ready.store(true, std::memory_order_release);
        // Last pixel access precedes release of the snapshot slot.
        busy.store(false, std::memory_order_release);
    }
    bool consume(uint64_t &value) {
        if (!ready.load(std::memory_order_acquire)) return false;
        value = mismatches;
        ready.store(false, std::memory_order_release);
        return true;
    }
};
}
