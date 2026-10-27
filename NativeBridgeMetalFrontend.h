// SPDX-License-Identifier: MIT
// Entry points of the native Metal frontend over the bridge (NativeBridgeMetalFrontend.mm).
#pragma once
#include <stddef.h>
#include <stdint.h>
#ifdef __OBJC__
#import <Foundation/Foundation.h>
extern "C" {
// Opens the process's one device: loader exports vkGetInstanceProcAddr (Turnip), cache is a writable shader-cache directory, registryID is the
// accelerator service's registry entry. missing (may be null) is told the class and selector of every unimplemented call.
id native_bridge_metal_open(const char *loader, const char *cache, uint64_t registryID, void (*missing)(const char *, const char *));
// Bare AIR bitcode as a library (the bridge's fixtures; Metal itself accepts only metallibs).
id native_bridge_metal_library_with_air(const void *bytes, size_t length);
// One line for the plugin's status report: command buffers committed/failed locally, pipelines made, refusals, the last refusal's text.
size_t native_bridge_metal_status(char *out, size_t size);
// Default off. Enable before device publication. Raw mach_absolute_time ticks;
// drain only from the status thread, never from commit/completion callbacks.
void native_bridge_metal_enable_latency(bool enabled);
void native_bridge_metal_latency_record(uint32_t kind,uint64_t id,uint64_t tick);
bool native_bridge_metal_latency_event(uint64_t out[4]);
uint64_t native_bridge_metal_latency_lost(void);
// Optional diagnostic, null by default. Install before device publication. The
// first two render-pipeline failures call it synchronously, outside telemetry
// locks; strings are borrowed only for the call. It never changes the result.
typedef void (*NativeMetalPipelineFailureCallback)(unsigned sequence, const char *stage, const char *error, const char *spec);
void native_bridge_metal_set_pipeline_failure_callback(NativeMetalPipelineFailureCallback callback);
// Optional raw no-copy refusal diagnostic, null by default. Install before
// publication. First four refusals only, synchronously after telemetry locks
// and runtime import gates are released. Error is borrowed for this call only;
// pointer is an address to print, never memory the callback may read or pin.
typedef void (*NativeMetalNoCopyFailureCallback)(unsigned sequence, uintptr_t pointer, uint64_t length, uint64_t options, const char *error);
void native_bridge_metal_set_no_copy_failure_callback(NativeMetalNoCopyFailureCallback callback);
// Default off; configure before opening the process device. Native backend family support is preserved.
void native_bridge_metal_require_fetch_for_apple3(bool enabled);
// Default off; configure before device publication. Compile completion-form
// requests on one worker and deliver handlers on a separate queue. This is an
// opt-in contract trial, not evidence that a particular UI stall is resolved.
void native_bridge_metal_set_async_creation(bool enabled);
// The pixel formats of the textures asked for (first eight distinct) and how many texture requests the runtime refused.
size_t native_bridge_metal_formats(char *out, size_t size);
size_t native_bridge_metal_telemetry(char *out, size_t size);
// Structured cumulative artifact/driver-cache counters for offline qualification.
struct NmbShaderCacheStats;
bool native_bridge_metal_shader_cache_stats(struct NmbShaderCacheStats *out);
bool native_bridge_metal_work_stats(uint64_t out[10]);
// The same fixed CPU, memory and pipeline workload on the tablet and the host, so their times compare directly (int-145): compute, first-touch
// page faults, memset bandwidth, CommonCrypto SHA-256, then the first `pairs` vertex/fragment pairs of the given QuartzCore metallib made into
// BGRA8 render states and drawn once each (pipeline_timing_host's draw=1 order). The results become the telemetry's Y rows; also returned.
size_t native_bridge_metal_benchmark(id device, const char *metallib, unsigned pairs, char *out, size_t size);
}
#endif
