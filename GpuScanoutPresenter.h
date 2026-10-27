// SPDX-License-Identifier: MIT
// GPU presentation: the GPU copies a finished BGRA frame (the compositor's IOSurface pages) into the firmware scanout memory. Both are
// imported without a copy (newBufferWithBytesNoCopy: pinned by the kernel and mapped into the GPU's 4 KiB context), the copy is a blit on
// the GPU's queue, and the call returns after the GPU retired it. No CPU touches a pixel; there is no staging copy and no software fallback
// here. Used through the public Metal API, on any device (the native frontend on the tablet, the Mac's device for the host check).
#pragma once
#import <Metal/Metal.h>
#include <unistd.h>
#include <stdint.h>
#include <string.h>
#include "ScanoutCopyTrace.h"

namespace gpupresent {

struct Scanout {
    void *base;       // writable mapping of the firmware framebuffer (page aligned)
    size_t bytes;     // whole extent, a multiple of the page size
    size_t stride;    // bytes per row
    uint32_t width, height;
};

struct Frame {
    const void *base; // page-aligned mapping of the surface pages
    size_t bytes;     // allocation size, a multiple of the page size
    size_t stride;
    uint32_t width, height;
};

enum Result { kOk = 0, kBadFrame, kBadScanout, kNoResource, kGpuFailed };

class Presenter {
public:
    Presenter(id<MTLDevice> device, const Scanout &scanout) : device_(device), scanout_(scanout) {
        queue_ = [device newCommandQueue];
        if (!validScanout() || !queue_) return;
        target_ = [device newBufferWithBytesNoCopy:scanout.base length:scanout.bytes options:MTLResourceStorageModeShared deallocator:nil];
    }
    bool ready() const { return target_ != nil; }
    // One frame: encodes the GPU copy, commits, waits for the GPU. Rows are copied as whole rows of width*4 bytes.
    Result present(const Frame &frame, CopyTrace *trace = nullptr) {
        if (!ready()) return kBadScanout;
        const size_t page = (size_t)getpagesize(), row = (size_t)frame.width * 4;
        if (!frame.base || ((uintptr_t)frame.base & (page - 1)) || !frame.bytes || (frame.bytes & (page - 1)) ||
            frame.width != scanout_.width || frame.height != scanout_.height || frame.stride < row ||
            (size_t)frame.height > frame.bytes / frame.stride || frame.stride * (frame.height - 1) + row > frame.bytes) return kBadFrame;
        const bool tracing = trace && trace->begin(frame.width, frame.height, frame.stride, scanout_.stride);
        struct TraceReturn {
            CopyTrace *trace;
            ~TraceReturn() { if (trace) trace->returned(); }
        } traceReturn{tracing ? trace : nullptr};
        id<MTLBuffer> source = [device_ newBufferWithBytesNoCopy:(void *)frame.base length:frame.bytes options:MTLResourceStorageModeShared deallocator:nil];
        if (tracing) trace->reached(2); // source import returned
        if (!source) return kNoResource;
        id<MTLCommandBuffer> commands = [queue_ commandBuffer];
        if (tracing) trace->encoded(1);
        struct EncoderTraceScope {
            CopyTrace *trace;
            explicit EncoderTraceScope(CopyTrace *t) : trace(t) { if (trace) trace->encoderEnter(); }
            ~EncoderTraceScope() { if (trace) trace->encoderLeave(); }
        };
        id<MTLBlitCommandEncoder> blit = nil;
        {
            EncoderTraceScope encoderTrace(tracing ? trace : nullptr);
            blit = commands ? [commands blitCommandEncoder] : nil;
        }
        if (tracing) trace->encoded(2);
        if (!blit) return kNoResource;
        if (frame.stride == scanout_.stride) {
            if (tracing) trace->route(0);
            if (tracing) trace->encoded(3);
            // Equal pitch: the rows are one contiguous run.
            [blit copyFromBuffer:source sourceOffset:0 toBuffer:target_ destinationOffset:0 size:frame.stride * (frame.height - 1) + row];
        } else {
            // A linear texture over the surface pages reads them with the surface's pitch and writes the scanout's pitch.
            MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm width:frame.width
                                                                                       height:frame.height mipmapped:NO];
            d.storageMode = MTLStorageModeShared;
            d.usage = MTLTextureUsageShaderRead;
            const NSUInteger alignment = [device_ minimumLinearTextureAlignmentForPixelFormat:MTLPixelFormatBGRA8Unorm];
            if (tracing) trace->route(frame.stride % alignment ? 1 : 2, alignment);
            if (frame.stride % alignment) {
                if (tracing) trace->encoded(3);
                for (uint32_t y = 0; y < frame.height; ++y)
                    [blit copyFromBuffer:source sourceOffset:y * frame.stride toBuffer:target_ destinationOffset:y * scanout_.stride size:row];
            } else {
                id<MTLTexture> texture = [source newTextureWithDescriptor:d offset:0 bytesPerRow:frame.stride];
                if (tracing) trace->encoded(3);
                if (!texture) {[blit endEncoding]; return kNoResource;}
                [blit copyFromTexture:texture sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0, 0, 0)
                           sourceSize:MTLSizeMake(frame.width, frame.height, 1) toBuffer:target_ destinationOffset:0
                    destinationBytesPerRow:scanout_.stride destinationBytesPerImage:scanout_.stride * frame.height];
            }
        }
        if (tracing) trace->encoded(4);
        [blit endEncoding];
        if (tracing) trace->encoded(5);
        if (tracing) trace->reached(3); // encoding returned
        [commands commit];
        if (tracing) trace->reached(4); // commit returned
        [commands waitUntilCompleted];
        if (tracing) trace->reached(5); // wait returned; status is checked below
        return commands.status == MTLCommandBufferStatusCompleted ? kOk : kGpuFailed;
    }
private:
    bool validScanout() const {
        const size_t page = (size_t)getpagesize();
        return scanout_.base && !((uintptr_t)scanout_.base & (page - 1)) && scanout_.bytes && !(scanout_.bytes & (page - 1)) &&
               scanout_.width && scanout_.height && scanout_.stride >= (size_t)scanout_.width * 4 &&
               scanout_.stride * scanout_.height <= scanout_.bytes;
    }
    id<MTLDevice> device_;
    Scanout scanout_;
    id<MTLCommandQueue> queue_;
    id<MTLBuffer> target_;
};

}  // namespace gpupresent
