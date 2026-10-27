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
#include "ScanoutVerification.h"
#include <memory>
#include <dispatch/dispatch.h>

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
    // Diagnostic storage is allocated before WAIT_SWAP, never while a frame is leased.
    // Two packed, private-to-this-client snapshots: source and actual scanout.
    bool prepareVerification() {
        const size_t row = (size_t)scanout_.width * 4;
        verificationRows_ = scanout_.height < 400 ? scanout_.height : 400;
        if (!ready() || !row || verificationRows_ > SIZE_MAX / row / 2) return false;
        verificationBytes_ = row * verificationRows_;
        verificationResult_ = std::make_shared<VerificationResult>();
        verification_ = [device_ newBufferWithLength:verificationBytes_ * 2 options:MTLResourceStorageModeShared];
        return verification_ && [verification_ contents];
    }
    // Capture while the swap lease still protects the source mapping and pixels.
    // No CPU framebuffer loads. This queue has retired the display copy already.
    bool verificationAvailable() const {
        return verificationResult_ && !verificationResult_->busy.load(std::memory_order_acquire) &&
            !verificationResult_->ready.load(std::memory_order_acquire);
    }
    bool captureVerification(const Frame &frame) {
        if (!verification_ || !verificationBytes_ || verificationResult_->busy.load(std::memory_order_acquire) ||
            verificationResult_->ready.load(std::memory_order_acquire)) return false;
        const size_t row = (size_t)frame.width * 4;
        if (frame.width != scanout_.width || frame.height != scanout_.height ||
            frame.stride < row || !frame.stride || frame.bytes / frame.stride < verificationRows_) return false;
        const NSUInteger alignment = [device_ minimumLinearTextureAlignmentForPixelFormat:MTLPixelFormatBGRA8Unorm];
        // Refuse unsupported geometry; never replace the diagnostic with a CPU copy.
        if (!alignment || frame.stride % alignment || scanout_.stride % alignment || row % alignment) return false;
        id<MTLBuffer> source = [device_ newBufferWithBytesNoCopy:(void *)frame.base length:frame.bytes options:MTLResourceStorageModeShared deallocator:nil];
        MTLTextureDescriptor *d = [MTLTextureDescriptor texture2DDescriptorWithPixelFormat:MTLPixelFormatBGRA8Unorm
                                     width:frame.width height:verificationRows_ mipmapped:NO];
        d.storageMode = MTLStorageModeShared; d.usage = MTLTextureUsageShaderRead;
        id<MTLTexture> expected = [source newTextureWithDescriptor:d offset:0 bytesPerRow:frame.stride];
        id<MTLTexture> shown = [target_ newTextureWithDescriptor:d offset:0 bytesPerRow:scanout_.stride];
        if (!source || !expected || !shown) return false;
        id<MTLCommandBuffer> commands = [queue_ commandBuffer];
        id<MTLBlitCommandEncoder> blit = [commands blitCommandEncoder];
        if (!commands || !blit) return false;
        [blit copyFromTexture:expected sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0)
                  sourceSize:MTLSizeMake(frame.width,verificationRows_,1) toBuffer:verification_ destinationOffset:0
                  destinationBytesPerRow:row destinationBytesPerImage:verificationBytes_];
        [blit copyFromTexture:shown sourceSlice:0 sourceLevel:0 sourceOrigin:MTLOriginMake(0,0,0)
                  sourceSize:MTLSizeMake(frame.width,verificationRows_,1) toBuffer:verification_ destinationOffset:verificationBytes_
                  destinationBytesPerRow:row destinationBytesPerImage:verificationBytes_];
        [blit endEncoding]; [commands commit]; [commands waitUntilCompleted];
        return commands.status == MTLCommandBufferStatusCompleted;
    }
    // No live mappings or Client/Presenter pointers escape to this worker. The
    // block retains both storage and result even if the serving client exits.
    void compareVerificationAsync() {
        auto result = verificationResult_;
        id<MTLBuffer> storage = verification_;
        const uint32_t width = scanout_.width, rows = verificationRows_;
        result->busy.store(true, std::memory_order_release);
        dispatch_async(dispatch_get_global_queue(DISPATCH_QUEUE_PRIORITY_DEFAULT, 0), ^{
            result->publish(VerificationResult::compare((const uint32_t *)[storage contents], width, rows));
        });
    }
    bool consumeVerification(uint64_t &mismatches) {
        return verificationResult_ && verificationResult_->consume(mismatches);
    }
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
    id<MTLBuffer> verification_ = nil;
    std::shared_ptr<VerificationResult> verificationResult_;
    size_t verificationBytes_ = 0;
    uint32_t verificationRows_ = 0;
};

}  // namespace gpupresent
