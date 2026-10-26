// SPDX-License-Identifier: MIT
// The throw site of an uncaught Objective-C exception (int-201). MobileSafari aborts with "-[_NSPlaceholderData initWithContentsOfURL:...]: nil URL
// argument" on a dispatch worker, and the crash record's backtrace has only the dispatch frames: the unwinder gave up inside the throw, so which
// caller passed the nil URL is lost. Foundation records the return addresses when it throws (callStackReturnAddresses); this handler copies them,
// with the exception's name and reason, into a malloc'd C string that the kernel's abort-reason scan finds in the dying process's heap
// (kits/sheng/BootFaultProbe.c, "SHENGTHROW ") and the log prints as the THROW row. The original handler (the app's own, or Foundation's) runs
// after it, unchanged; a process that never throws is untouched.
#pragma once
#import <Foundation/Foundation.h>
#include <stdio.h>
#include <unistd.h>
#include <stdlib.h>
#include <string.h>

namespace exceptiondiag {

static NSUncaughtExceptionHandler *gPrevious;
static char *gText;   // kept referenced: the text must stay in the heap until the process is gone
using Output = void (*)(const char *);
static Output gOutput; // null by default; the board must explicitly opt in
static unsigned gDirectReported;
static thread_local bool gHandling;

static inline void setOutput(Output output) {
    __atomic_store_n(&gOutput, output, __ATOMIC_RELEASE);
}

// No Foundation calls or allocation in the direct formatter. A callback may
// fail during process teardown; diagnostics must never suppress the original handler.
static void emitText(const char *text) noexcept {
    Output output = __atomic_load_n(&gOutput, __ATOMIC_ACQUIRE);
    if (!output || __atomic_exchange_n(&gDirectReported, 1u, __ATOMIC_ACQ_REL)) return;
    @try {
        try {
            constexpr unsigned maxLines = 16;
            constexpr unsigned payload = 40;
            const char *source = text ? text : "SHENGTHROW <format unavailable>";
            size_t length = strnlen(source, maxLines * payload + 1);
            const bool truncated = length > maxLines * payload;
            if (truncated) length = maxLines * payload;
            unsigned lines = unsigned((length + payload - 1) / payload);
            if (!lines) lines = 1;
            for (unsigned i = 0; i < lines; ++i) {
                char clean[payload + 1] = {};
                const size_t offset = size_t(i) * payload;
                size_t count = length > offset ? length - offset : 0;
                if (count > payload) count = payload;
                for (size_t c = 0; c < count; ++c) {
                    const unsigned char value = (unsigned char)source[offset + c];
                    clean[c] = value >= 32 && value < 127 ? char(value) : '?';
                }
                // Fixed-width PID as raw bits, including any high bit: 22-byte
                // prefix + 40-byte payload stays below the board's 63-byte limit.
                char line[64];
                snprintf(line, sizeof line, "THROW %08x %c%c/%c%c%c %s", unsigned(getpid()),
                         '0' + (i + 1) / 10, '0' + (i + 1) % 10,
                         '0' + lines / 10, '0' + lines % 10,
                         truncated && i + 1 == lines ? '+' : ' ', clean);
                output(line);
            }
        } catch (...) { /* A C++ sink failure cannot replace the original exception. */ }
    } @catch (NSException *) { /* Likewise for an Objective-C sink failure. */ }
}

static void handler(NSException *exception) {
    if (gHandling) return; // A failing formatter/sink or previous handler must not recurse.
    gHandling = true;
    @try {
        try {
            @try {
                char *captured = nullptr;
                @autoreleasepool {
                    NSMutableString *text = [NSMutableString stringWithFormat:@"SHENGTHROW %@ | ", exception.name];
                    NSString *reason = exception.reason ?: @"";
                    [text appendString:reason.length > 96 ? [reason substringToIndex:96] : reason];
                    [text appendString:@" |"];
                    NSArray<NSNumber *> *frames = exception.callStackReturnAddresses;
                    for (NSUInteger i = 0; i < frames.count && i < 20; ++i) [text appendFormat:@" %llx", (unsigned long long)[frames objectAtIndex:i].unsignedLongLongValue];
                    for (NSUInteger i = 0; i < text.length; ++i) if ([text characterAtIndex:i] < 32 || [text characterAtIndex:i] > 126) [text replaceCharactersInRange:NSMakeRange(i, 1) withString:@"?"];
                    if (const char *utf8 = text.UTF8String) {
                        captured = strdup(utf8);
                        if (captured) __atomic_store_n(&gText, captured, __ATOMIC_RELEASE);
                    }
                }
                emitText(captured);
            } @catch (NSException *) {
                emitText(nullptr);
            }
        } catch (...) {
            emitText(nullptr);
        }
        if (gPrevious) gPrevious(exception);
    } @finally {
        gHandling = false;
    }
}
// Every process that loads the plugin, once: the handler is installed in front of whatever the process already had.
static void start() {
    static bool started;
    if (started) return;
    started = true;
    gPrevious = NSGetUncaughtExceptionHandler();
    NSSetUncaughtExceptionHandler(handler);
}

}  // namespace exceptiondiag
