#!/usr/bin/env python3
"""Execute the production fixed record with a deterministic clock and concurrent reader."""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
root = Path(__file__).resolve().parents[1]
source = r'''
#include "ScanoutCopyTrace.h"
#include <cassert>
#include <thread>
#include <atomic>
static uint64_t tick=1000;
static unsigned calls=0;
static uint64_t clockUs() { ++calls; return tick; }
int main() {
    gpupresent::CopyTrace off;
    assert(!off.begin()); assert(!calls); assert(!off.snapshot(0).phase);
    gpupresent::CopyTrace trace; trace.configure(clockUs);
    assert(trace.begin()); assert(calls==1);
    assert(trace.snapshot(999).elapsed==0); // reader clock can precede publication
    auto s=trace.snapshot(1400); assert(s.phase==1 && s.elapsed==400);
    tick=1600; trace.reached(2);
    s=trace.snapshot(2000); assert(s.phase==2 && s.intervals[0]==600 && s.intervals[1]==0);
    std::atomic<bool> done{false}, reading{false};
    std::thread reader([&] {
        while(!done.load()) {
            auto t=trace.snapshot(10000);
            reading.store(true);
            assert(t.phase>=2 && t.phase<=5);
            assert(t.intervals[0]==600);
            if(t.phase>=3) assert(t.intervals[1]==700);
            if(t.phase>=4) assert(t.intervals[2]==800);
            if(t.phase==5) assert(t.intervals[3]==900 && t.elapsed==3000);
        }
    });
    while(!reading.load()) std::this_thread::yield();
    tick=2300; trace.reached(3); tick=3100; trace.reached(4);
    tick=4000; trace.reached(5); done.store(true); reader.join();
    s=trace.snapshot(999999); assert(s.phase==5 && s.elapsed==3000);
    assert(!trace.begin()); assert(calls==5); // subsequent frames cannot overwrite
    auto after=trace.snapshot(9999999); assert(after.elapsed==s.elapsed);
}
'''
with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    (tmp/'test.cpp').write_text(source)
    subprocess.run([os.environ.get('CXX','c++'), '-std=c++17', '-pthread', '-Wall', '-Wextra', '-Werror',
                    '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-I', str(root),
                    str(tmp/'test.cpp'), '-o', str(tmp/'test')], check=True)
    subprocess.run([str(tmp/'test')], check=True)
# Compile the actual Objective-C++ presenter against a bounded Metal API mock.
# This checks the changed call signature and hooks; it is not an Apple SDK build.
metal = r'''
#include <stddef.h>
#include <stdint.h>
#define nil ((id)0)
#define NO false
typedef unsigned long NSUInteger;
enum { MTLResourceStorageModeShared=0, MTLStorageModeShared=0, MTLTextureUsageShaderRead=1,
       MTLPixelFormatBGRA8Unorm=80, MTLCommandBufferStatusCompleted=4 };
struct MTLOrigin { NSUInteger x,y,z; };
struct MTLSize { NSUInteger width,height,depth; };
static inline MTLOrigin MTLOriginMake(NSUInteger x, NSUInteger y, NSUInteger z) { return {x,y,z}; }
static inline MTLSize MTLSizeMake(NSUInteger x, NSUInteger y, NSUInteger z) { return {x,y,z}; }
__attribute__((objc_root_class)) @interface NSObject @end
@interface MTLTextureDescriptor : NSObject
+ (instancetype)texture2DDescriptorWithPixelFormat:(NSUInteger)f width:(NSUInteger)w height:(NSUInteger)h mipmapped:(bool)m;
@property NSUInteger storageMode,usage;
@end
@protocol MTLTexture @end
@protocol MTLBuffer
- (id<MTLTexture>)newTextureWithDescriptor:(MTLTextureDescriptor *)d offset:(NSUInteger)o bytesPerRow:(NSUInteger)b;
@end
@protocol MTLBlitCommandEncoder
- (void)copyFromBuffer:(id<MTLBuffer>)s sourceOffset:(NSUInteger)o toBuffer:(id<MTLBuffer>)d destinationOffset:(NSUInteger)p size:(NSUInteger)n;
- (void)copyFromTexture:(id<MTLTexture>)s sourceSlice:(NSUInteger)i sourceLevel:(NSUInteger)l sourceOrigin:(MTLOrigin)o sourceSize:(MTLSize)z toBuffer:(id<MTLBuffer>)d destinationOffset:(NSUInteger)p destinationBytesPerRow:(NSUInteger)r destinationBytesPerImage:(NSUInteger)b;
- (void)endEncoding;
@end
@protocol MTLCommandBuffer
- (id<MTLBlitCommandEncoder>)blitCommandEncoder;
- (void)commit;
- (void)waitUntilCompleted;
@property(readonly) NSUInteger status;
@end
@protocol MTLCommandQueue
- (id<MTLCommandBuffer>)commandBuffer;
@end
@protocol MTLDevice
- (id<MTLCommandQueue>)newCommandQueue;
- (id<MTLBuffer>)newBufferWithBytesNoCopy:(void *)p length:(NSUInteger)n options:(NSUInteger)o deallocator:(void *)d;
- (NSUInteger)minimumLinearTextureAlignmentForPixelFormat:(NSUInteger)f;
@end
'''
with tempfile.TemporaryDirectory() as directory:
    tmp=Path(directory); (tmp/'Metal').mkdir(); (tmp/'Metal/Metal.h').write_text(metal)
    (tmp/'presenter.mm').write_text('#include "GpuScanoutPresenter.h"\nvoid check(gpupresent::Presenter &p, const gpupresent::Frame &f, gpupresent::CopyTrace &t) { p.present(f); p.present(f,&t); }\n')
    includes=[]
    if sys.platform.startswith('linux'):
        search=subprocess.run([os.environ.get('CXX','c++'), '-E', '-v', '-x', 'c++', '-'], input='', text=True, capture_output=True, check=True).stderr
        for line in search.split('#include <...> search starts here:')[1].split('End of search list.')[0].splitlines():
            if line.strip(): includes += ['-isystem', line.strip()]
    subprocess.run([os.environ.get('CLANGXX','clang++'), *includes, '-std=c++17', '-x', 'objective-c++',
                    '-fsyntax-only', '-Wall', '-Wextra', '-Werror', '-I', str(tmp), '-I', str(root),
                    str(tmp/'presenter.mm')], check=True)

# Verify the production hooks delimit the actual operations, including all encoding branches.
presenter=(root/'GpuScanoutPresenter.h').read_text()
ordered=['trace->begin()', 'newBufferWithBytesNoCopy:(void *)frame.base', 'trace->reached(2)',
         '[queue_ commandBuffer]', '[blit endEncoding];\n        if (tracing)', 'trace->reached(3)',
         '[commands commit]', 'trace->reached(4)', '[commands waitUntilCompleted]', 'trace->reached(5)']
positions=[presenter.index(x) for x in ordered]
assert positions == sorted(positions)
client=(root/'GpuScanoutClient.h').read_text()
assert 'swap.width, swap.height}, copyTrace_)' in client
plugin=(root/'NativeVulkanMetalPlugin.mm').read_text()
assert 'diag && !strcmp(diag, "1")' in plugin
assert 'if (gCopyTraceEnabled)' in plugin
assert 'transport, &st, false, &gCopyTrace)' in plugin
assert len('CT i4294967295 n4294967295 c4294967295 w4294967295') <= 62
print('PASS: default off, partial stages, concurrent snapshots, clock publication race, first-copy immutability, production hooks, bounded lines')
