// SPDX-License-Identifier: MIT
// Opt-in memory-scaled pool blocks of the metal2vulkan runtime (bridge/patches/0008-optional-memory-pool-size.patch reads
// NMB_POOL_BLOCK_MIB = 4, 8, 16, 32 or 64; anything else keeps the upstream 64 MiB). NMB_POOL_BLOCK_MIB=auto is resolved here, by the
// frontend before it opens the device: about 1/128 of the physical memory, rounded down to a power of two, from 4 to 64 MiB
// (1-2 GiB: 8 MiB, 2-4 GiB: 16 MiB, 4-8 GiB: 32 MiB, 8 GiB or more, or unknown: 64 MiB). The runtime keeps half a block free in each
// pool it places its own resources in, and some drivers (PowerVR's) wire a buffer object's pages when it is created, so on small
// devices the block size sets a floor of wired memory. Requests larger than half a block still take dedicated allocations.
#pragma once
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static inline unsigned nmb_pool_block_mib_for_memsize(uint64_t memsize) {
    if (!memsize) return 64;
    const uint64_t target = memsize / 128;
    unsigned mib = 4;
    while (mib < 64 && ((uint64_t)mib * 2 << 20) <= target) mib *= 2;
    return mib;
}

// Replace NMB_POOL_BLOCK_MIB=auto by the size for memsize (0: unknown). Returns the size set, or 0 when the variable is not "auto".
static inline unsigned nmb_pool_block_resolve_auto(uint64_t memsize) {
    const char *value = getenv("NMB_POOL_BLOCK_MIB");
    if (!value || strcmp(value, "auto")) return 0;
    const unsigned mib = nmb_pool_block_mib_for_memsize(memsize);
    char text[4];
    snprintf(text, sizeof text, "%u", mib);
    setenv("NMB_POOL_BLOCK_MIB", text, 1);
    return mib;
}
