// SPDX-License-Identifier: MIT
// Vendor-neutral Metal plugin: the native Metal frontend (NativeBridgeMetalFrontend.mm, the metal2vulkan runtime) on any Vulkan driver
// that exports vkGetInstanceProcAddr (iPadOS has no Khronos loader; Mesa's drivers export it with their Darwin kernel interfaces),
// reached through the original discovery (IOAcceleratorES -> MetalPluginName -> NSBundle -> principal class ->
// initWithAcceleratorPort:). Everything a board decides is behind NativeMetalBoard.h (the driver's environment, the progress log and
// the display hand-off transport); scripts/build-vulkan-metal-plugin.py compiles this file with one board source, the principal
// class NATIVE_METAL_PLUGIN_CLASS and the driver library NATIVE_METAL_VULKAN_DRIVER (absolute, or relative to the bundle directory).
// The Adreno plugin (NativeAdrenoMetalPlugin.mm) is separate and unchanged.
//
// Files beside the bundle (<Name> is the bundle executable's name; the union view of /System/Library/Extensions carries them):
//   <Name>.publish   backboardd returns the native device to the discovery, which registers it, and the render server composites the
//                    system UI through it; the display driver's offered frames are copied into the scanout by the GPU
//                    (GpuScanoutClient.h) when the board offers a hand-off transport.
//   <Name>.clients   other processes that get their own native device (names separated by blanks, commas or new lines; "*" for all).
//   <Name>.selftest  without .publish: backboardd runs the bridge self-test (nmb_selftest: a compute kernel, a render pass, a render
//                    into imported host pages and the translation of QuartzCore's own library) and returns no device, so the original
//                    software renderer keeps running. The first thing to run on new hardware.
//   <Name>.prime     with .publish: every Metal library of the OS goes through the runtime 90 s after the device opened (PrimeShaders.h),
//                    with the list <Name>.primelist when present.
// Without these files the plugin returns nil in every process.
//
// Markers (native_metal_board_mark): 0x6800 published mode entered, 0x6801 driver library found, 0x6802 device made, 0x6803 device
// returned, 0x6810+n cache directory n served, 0x68f1 no driver library, 0x68f2 no device; 0x6500 self-test started, 0x6501..0x650f
// the bridge's steps, 0x65f0 passed, 0x65f1 failed; 0x6700 scanout service started, 0x6710..0x6731 the client's steps (GpuScanoutClient.h).
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <dlfcn.h>
#include <fcntl.h>
#include <pthread.h>
#include <pwd.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include "ScanoutCopyTrace.h"
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <unistd.h>
#include <string>
#include "bridge/NativeMetalBridge.h"
#include "bridge/NativeMetalObjects.h"
#include "NativeBridgeMetalFrontend.h"
#include "NativeMetalBoard.h"
#include "PrimeShaders.h"
#include "ExceptionDiag.h"

#ifndef NATIVE_METAL_PLUGIN_CLASS
#error "NATIVE_METAL_PLUGIN_CLASS names the principal class (the accelerator's MetalPluginClassName)"
#endif
#ifndef NATIVE_METAL_VULKAN_DRIVER
#error "NATIVE_METAL_VULKAN_DRIVER names the Vulkan driver library (absolute, or relative to the bundle directory)"
#endif

@interface _MTLDevice : NSObject
@end
@interface NativeVulkanMetalLock : NSObject   // only a lock object for the one-time setup
@end
@implementation NativeVulkanMetalLock
@end
@interface NATIVE_METAL_PLUGIN_CLASS : _MTLDevice
- (id)initWithAcceleratorPort:(unsigned)port;
@end

// Board-owned opt-in. The generic plugin and Adreno handler keep heap-only
// behavior unless a board explicitly connects an existing text transport.
extern "C" void native_metal_set_exception_output(void (*output)(const char *)) {
    exceptiondiag::setOutput(output);
}

namespace vplugin {
// ---- reports ----
__attribute__((format(printf, 1, 2))) static void say(const char *format, ...) {
    char line[64];
    va_list arguments;
    va_start(arguments, format);
    vsnprintf(line, sizeof line, format, arguments);
    va_end(arguments);
    native_metal_board_text(line);
}
static void mark(uint32_t value) {native_metal_board_mark(value);}

// First copy only, enabled by the existing board pipeline diagnostic policy.
static gpupresent::CopyTrace gCopyTrace;
static bool gCopyTraceEnabled = false;
static uint64_t copyClockUs() {
    static const mach_timebase_info_data_t base = [] { mach_timebase_info_data_t b{}; mach_timebase_info(&b); return b; }();
    const uint64_t ticks = mach_absolute_time();
    return (ticks / base.denom * base.numer + ticks % base.denom * base.numer / base.denom) / 1000;
}
static uint32_t copyMs(uint64_t us) { return (uint32_t)(us/1000 > UINT32_MAX ? UINT32_MAX : us/1000); }
// ---- paths ----
// The bundle's executable (<dir>/<Name>.bundle/<Name>): the bundle directory, the directory that holds it and <Name>.
static bool locate(std::string &bundle, std::string &parent, std::string &name) {
    Dl_info info = {};
    if (!dladdr(reinterpret_cast<const void *>(&locate), &info) || !info.dli_fname) return false;
    const std::string executable = info.dli_fname;
    const size_t slash = executable.rfind('/');
    if (slash == std::string::npos || slash == 0) return false;
    bundle = executable.substr(0, slash);
    name = executable.substr(slash + 1);
    const size_t up = bundle.rfind('/');
    if (up == std::string::npos) return false;
    parent = bundle.substr(0, up);
    return !name.empty();
}
// The file <Name><suffix> beside the bundle, when it is readable.
static bool beside(const char *suffix, std::string &path) {
    std::string bundle, parent, name;
    if (!locate(bundle, parent, name)) return false;
    path = parent + "/" + name + suffix;
    return access(path.c_str(), R_OK) == 0;
}
static bool requested(const char *suffix) {
    std::string path;
    return beside(suffix, path);
}
static std::string driverPath() {
    const char *driver = NATIVE_METAL_VULKAN_DRIVER;
    if (driver[0] == '/') return driver;
    std::string bundle, parent, name;
    return locate(bundle, parent, name) ? bundle + "/" + driver : std::string();
}
// Whether <Name>.clients names this process.
static bool listedClient() {
    std::string path;
    if (!beside(".clients", path)) return false;
    FILE *file = fopen(path.c_str(), "r");
    if (!file) return false;
    char text[1024] = {};
    const size_t length = fread(text, 1, sizeof text - 1, file);
    fclose(file);
    text[length] = 0;
    const char *self = getprogname();
    for (char *save = nullptr, *name = strtok_r(text, " ,\t\r\n", &save); name; name = strtok_r(nullptr, " ,\t\r\n", &save))
        if (!strcmp(name, "*") || !strcmp(name, self)) return true;
    return false;
}
static uint64_t registryID(unsigned port) {
    uint64_t id = 0;
    if (void *iokit = dlopen("/System/Library/Frameworks/IOKit.framework/IOKit", RTLD_NOW | RTLD_LOCAL))
        if (auto entryID = (int (*)(unsigned, uint64_t *))dlsym(iokit, "IORegistryEntryGetRegistryEntryID")) entryID(port, &id);
    return id;
}
// Writable shader-cache directories this process may use, best first (the render server's sandbox decides which work; the user temp
// directory of the mobile user is emptied at every boot, so the render server tries its Library/Caches first).
static unsigned cacheCandidates(bool server, std::string out[5]) {
    unsigned n = 0;
    char path[512];
    if (server) {
        if (const passwd *user = getpwuid(getuid()); user && user->pw_dir && user->pw_dir[0]) {
            snprintf(path, sizeof path, "%s/Library/Caches/com.apple.backboardd", user->pw_dir);
            mkdir(path, 0700);
            out[n++] = std::string(path) + "/";
        }
    }
    if (confstr(_CS_DARWIN_USER_CACHE_DIR, path, sizeof path)) out[n++] = path;
    if (confstr(_CS_DARWIN_USER_TEMP_DIR, path, sizeof path)) out[n++] = path;
    if (server) {
        out[n++] = "/tmp/";
        out[n++] = "/private/var/tmp/";
    }
    return n;
}
static unsigned gMissingReports;
static void missingSelector(const char *cls, const char *selector) {
    if (gMissingReports < 8) {
        ++gMissingReports;
        say("FM %.20s %.36s", cls ? cls : "?", selector ? selector : "?");
    }
}

// ---- the device ----
static std::string gCacheDirectory;
static id openDevice(unsigned port, bool server) {
    if (server) mark(0x6800);
    const std::string driver = driverPath();
    if (driver.empty() || access(driver.c_str(), R_OK) != 0) {
        mark(0x68f1);
        say("no driver %.50s", driver.c_str());
        return nil;
    }
    mark(0x6801);
    // IOSurfaces this GPU reads or writes must be plain rows (no Apple GPU compression); the render server picks a compressed
    // framebuffer unless told not to, and the display drivers here take plain 8-bit rows.
    CFPreferencesSetAppValue(CFSTR("CAEnableIOSurfaceCompression"), kCFBooleanFalse, kCFPreferencesCurrentApplication);
    if (server) {
        setenv("CA_DISABLE_FRAMEBUFFER_COMPRESSION", "1", 1);
        CFPreferencesSetAppValue(CFSTR("CAEnableFramebufferCompression"), kCFBooleanFalse, kCFPreferencesCurrentApplication);
        // Each VkPipeline making's time to the device's sink (the status line's counts).
        setenv("METAL2VULKAN_LOG_PIPELINES", "1", 0);
    }
    const uint64_t registry = registryID(port);
    std::string candidates[5];
    const unsigned count = cacheCandidates(server, candidates);
    id device = nil;
    const char *lastError = "no cache directory";
    for (unsigned i = 0; i < count && !device; ++i) {
        std::string directory = candidates[i] + "m2v";
        mkdir(directory.c_str(), 0700);
        if (access(directory.c_str(), W_OK) != 0) continue;
        @try {
            device = native_bridge_metal_open(driver.c_str(), directory.c_str(), registry, missingSelector);
        } @catch (NSException *exception) {
            device = nil;
        }
        if (device) {
            mark(0x6810 + i);
            gCacheDirectory = directory;
        } else {
            lastError = nmb_last_error();
        }
    }
    if (!device) {
        mark(0x68f2);
        say("no device: %.50s", lastError ? lastError : "");
        return nil;
    }
    mark(0x6802);
    return device;
}

// ---- the render server's threads ----
// The display driver's frames, copied into the scanout by the GPU (GpuScanoutClient.h) through the board's transport. One line when the
// state changes: "P<step> e<hex>", then "PF <frames>/<failures> r<result> v<verified>/<differing rows> t<ms> a<mean ms>" at the
// first frame, every fifth after and the first failure, "PX ..." when serving stops.
static void serveScanout(id device) {
    mark(0x6700);
    gpuscanout::Transport transport = {};
    if (!native_metal_board_scanout(&transport)) {
        say("P- no hand-off");
        return;
    }
    gpuscanout::Stats st = {};
    gpuscanout::Client client;
    const bool opened = client.open((id<MTLDevice>)device, transport, &st, gCopyTraceEnabled, &gCopyTrace);
    say("P%d e%x", st.step, (unsigned)st.error);
    if (!opened) return;
    uint64_t reportedFrames = 0, reportedFailures = 0;
    while (client.serveOne(500)) {
        const bool frame = st.frames != reportedFrames && (st.frames == 1 || st.frames - reportedFrames >= 5);
        if (frame || (st.failures && !reportedFailures)) {
            reportedFrames = st.frames ? st.frames : reportedFrames;
            reportedFailures = st.failures ? st.failures : reportedFailures;
            const uint64_t served = st.frames + st.failures;
            say("PF %llu/%llu r%d v%llu/%llu t%u a%llu", (unsigned long long)st.frames, (unsigned long long)st.failures, st.lastResult,
                (unsigned long long)st.verified, (unsigned long long)st.mismatchRows, st.lastMs,
                (unsigned long long)(served ? st.totalMs / served : 0));
        }
    }
    say("PX %llu/%llu r%d e%x", (unsigned long long)st.frames, (unsigned long long)st.failures, st.lastResult, (unsigned)st.error);
}
static id gServerDevice;
static void startServerThreads() {
    const char *diag = getenv("PVR_METAL_PIPELINE_DIAG");
    if (diag && !strcmp(diag, "1")) { gCopyTrace.configure(copyClockUs); gCopyTrace.configureEncoder(nmb_encoder_open_trace_set); gCopyTraceEnabled = true; }
    pthread_attr_t attributes;
    pthread_attr_init(&attributes);
    pthread_attr_setdetachstate(&attributes, PTHREAD_CREATE_DETACHED);
    pthread_attr_setstacksize(&attributes, 1u << 20);
    pthread_t thread;
    // The status line every 20 s: the formats asked for, then the bridge's counters in 57-character pieces.
    pthread_create(&thread, &attributes, [](void *) -> void * {
        unsigned phase = 0, statusSeconds = 0, reports = 0, lastCopyPhase = 0, lastEncoderMask = 0;
        bool lastReturned = false, copyReturned = false;
        for (;;) {
            // Until the first copy returns, opt-in observation must precede the
            // family's five-second timeout. Normal ST/PUB cadence stays 20 s.
            const unsigned seconds = gCopyTraceEnabled && !copyReturned ? 1 : 20-statusSeconds;
            sleep(seconds);
            statusSeconds += seconds;
            if (gCopyTraceEnabled) {
                const uint64_t encoderNow = copyClockUs();
                const auto copy = gCopyTrace.snapshot(encoderNow);
                // Sample after the snapshot: a concurrent event may have been
                // published after encoderNow but before the mask was read.
                const uint64_t intervalNow = copyClockUs();
                copyReturned = copy.returned;
                if (copy.phase && ((!copy.returned && reports < 8) || copy.phase != lastCopyPhase || copy.returned != lastReturned || copy.encoderMask != lastEncoderMask)) {
                    say("CG w%llu h%llu a%llu r%u", (unsigned long long)copy.geometry[0],
                        (unsigned long long)copy.geometry[1], (unsigned long long)copy.geometry[4], copy.route);
                    say("CS s%llu d%llu", (unsigned long long)copy.geometry[2], (unsigned long long)copy.geometry[3]);
                    say("CP%u e%u r%u", copy.phase, copyMs(copy.elapsed), copy.returned ? 1u : 0u);
                    say("CT i%u n%u c%u w%u", copyMs(copy.intervals[0]), copyMs(copy.intervals[1]),
                        copyMs(copy.intervals[2]), copyMs(copy.intervals[3]));
                    say("CE p%u q%u b%u t%u", copy.encodePhase, copyMs(copy.encoding[0]),
                        copyMs(copy.encoding[1]), copyMs(copy.encoding[2]));
                    say("CX x%u e%u", copyMs(copy.encoding[3]), copyMs(copy.encoding[4]));
                    const auto duration = [&](unsigned a, unsigned b) -> uint32_t {
                        return gpupresent::CopyTrace::encoderIntervalUs(copy, a, b, intervalNow);
                    };
                    say("EL m%08x l%u p%u", copy.encoderMask, duration(1,2), duration(3,4));
                    say("EP r%u c%u", duration(7,8), duration(9,10));
                    say("EA a%u b%u", duration(12,13), duration(14,15));
                    say("ES s%u f%u", duration(24,25), duration(1,22));
                    ++reports; lastCopyPhase = copy.phase; lastReturned = copy.returned; lastEncoderMask = copy.encoderMask;
                }
            }
            if (statusSeconds < 20) continue;
            statusSeconds %= 20;
            char full[420], line[64];
            native_bridge_metal_status(full, sizeof full);
            const size_t length = strlen(full);
            const unsigned pieces = (unsigned)((length + 56) / 57);
            const unsigned step = phase % (1 + (pieces ? (pieces > 7 ? 7 : pieces) : 1));
            if (step == 0) {
                native_bridge_metal_formats(line, sizeof line);
                say("PUB %.58s", line);
            } else {
                say("ST%u %.57s", step - 1, full + (size_t)(step - 1) * 57);
            }
            ++phase;
        }
        return nullptr;
    }, nullptr);
    pthread_create(&thread, &attributes, [](void *) -> void * {
        sleep(3);
        @try {
            serveScanout(gServerDevice);
        } @catch (NSException *exception) {
            say("PX exception %.40s", exception.name.UTF8String ?: "?");
        }
        return nullptr;
    }, nullptr);
    if (requested(".prime")) {
        // The translator recurses deeply on large functions: the stack of a main thread.
        pthread_attr_setstacksize(&attributes, 8u << 20);
        pthread_create(&thread, &attributes, [](void *) -> void * {
            std::string list;
            if (!beside(".primelist", list)) list.clear();
            prime::run((id<MTLDevice>)gServerDevice, gCacheDirectory, 90, list);
            return nullptr;
        }, nullptr);
    }
    pthread_attr_destroy(&attributes);
}

// ---- the self-test (no device registered) ----
static void *selftest(void *) {
    mark(0x6500);
    const std::string driver = driverPath();
    std::string candidates[5];
    const unsigned count = cacheCandidates(true, candidates);
    std::string cache;
    for (unsigned i = 0; i < count && cache.empty(); ++i) {
        std::string directory = candidates[i] + "m2v";
        mkdir(directory.c_str(), 0700);
        if (access(directory.c_str(), W_OK) == 0) cache = directory;
    }
    if (cache.empty()) {say("M no cache directory"); mark(0x65f1); return nullptr;}
    // The target's own compositor library, read in place.
    const uint8_t *qc = nullptr;
    size_t qcBytes = 0;
    const int fd = open("/System/Library/Frameworks/QuartzCore.framework/default.metallib", O_RDONLY);
    struct stat st = {};
    if (fd >= 0 && fstat(fd, &st) == 0 && st.st_size > 0) {
        void *m = mmap(nullptr, (size_t)st.st_size, PROT_READ, MAP_PRIVATE, fd, 0);
        if (m != MAP_FAILED) {qc = static_cast<const uint8_t *>(m); qcBytes = (size_t)st.st_size;}
    }
    if (fd >= 0) close(fd);
    NmbSelftest r = {};
    const uint32_t code = nmb_selftest(driver.c_str(), cache.c_str(), qc, qcBytes, mark, &r);
    if (qc) munmap(const_cast<uint8_t *>(qc), qcBytes);
    say("M s%x c%u r%u i%u q%u k%u/%u p%u/%u", r.stage, r.compute_bad, r.render_bad, r.import_bad, r.qc_functions, r.qc_kernels_ok,
        r.qc_kernels_failed, r.qc_pairs_ok, r.qc_pairs_failed);
    if (code) say("M %.60s", r.message);
    mark(!code && !r.compute_bad && !r.render_bad && !r.import_bad ? 0x65f0 : 0x65f1);
    return nullptr;
}
static void startSelftest() {
    pthread_attr_t attributes;
    pthread_attr_init(&attributes);
    pthread_attr_setdetachstate(&attributes, PTHREAD_CREATE_DETACHED);
    pthread_attr_setstacksize(&attributes, 8u << 20);
    pthread_t thread;
    if (pthread_create(&thread, &attributes, selftest, nullptr) != 0) say("M no thread");
    pthread_attr_destroy(&attributes);
}

static id deviceForPort(unsigned port) {
    static id device;
    static bool tried;
    @synchronized([NativeVulkanMetalLock class]) {
        if (tried) return device;
        tried = true;
        exceptiondiag::start();
        const bool server = strcmp(getprogname(), "backboardd") == 0;
        if (!requested(".publish")) {
            if (server && requested(".selftest")) {
                native_metal_board_prepare(port);
                startSelftest();
            }
            return nil;
        }
        if (!server && !listedClient()) return nil;
        native_metal_board_prepare(port);
        device = openDevice(port, server);
        if (device && server) {
            gServerDevice = device;
            startServerThreads();
            mark(0x6803);
        }
        return device;
    }
}
}  // namespace vplugin

@implementation NATIVE_METAL_PLUGIN_CLASS
- (id)initWithAcceleratorPort:(unsigned)port {
    self = [super init];
    if (!self || !port) return nil;
    // The original discovery registers what this returns (a subclass of _MTLDevice, the frontend's device) or nothing.
    return vplugin::deviceForPort(port);
}
@end
