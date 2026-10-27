#!/usr/bin/env python3
"""Execute the actual Presenter with GNU ObjC Metal mocks; no Apple/GPU claims."""
import ast, os, subprocess, tempfile
from pathlib import Path
root=Path(__file__).resolve().parents[1]
module=ast.parse((root/'tests/check_scanout_copy_trace.py').read_text())
metal=next(ast.literal_eval(n.value) for n in module.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='metal' for t in n.targets))
assert '@interface MTLTextureDescriptor : NSObject' in metal and '@interface NSObject @end' in metal
metal=metal.replace('@interface MTLTextureDescriptor : NSObject', '@interface MTLTextureDescriptor : NSObject { @public NSUInteger _storageMode, _usage, width_, height_; }')
metal=metal.replace('@interface NSObject @end', '@interface NSObject { void *isa; } @end')
source = r'''

#include <cassert>
#include <cstring>
#include <cstdlib>
#include "GpuScanoutPresenter.h"
extern "C" void *objc_getClass(const char *);
extern "C" void *class_createInstance(void *, size_t);
static unsigned descriptors, alignments, sourceImports, textures, copies;
static bool failTexture, failGpu, failBlit;
static void (*hook)(void *, uint32_t); static void *hookContext; static unsigned sets;
static uint64_t ticks;
static uint64_t testClock() {return ++ticks;}
static void install(void (*cb)(void *, uint32_t), void *ctx) {hook=cb;hookContext=ctx;++sets;}
@implementation NSObject @end
@implementation MTLTextureDescriptor
@synthesize storageMode=_storageMode, usage=_usage;
+ (instancetype)texture2DDescriptorWithPixelFormat:(NSUInteger)f width:(NSUInteger)w height:(NSUInteger)h mipmapped:(bool)m {
    assert(f==MTLPixelFormatBGRA8Unorm && !m); ++descriptors;
    MTLTextureDescriptor *d=(MTLTextureDescriptor *)class_createInstance(objc_getClass("MTLTextureDescriptor"),0);
    d->width_=w;d->height_=h;return d;
}
@end
@interface Buffer : NSObject <MTLBuffer> { @public unsigned char *bytes_; NSUInteger length_; }
@end
@interface Texture : NSObject <MTLTexture> { @public Buffer *buffer_; NSUInteger row_, width_, height_; }
@end
@implementation Buffer
- (id<MTLTexture>)newTextureWithDescriptor:(MTLTextureDescriptor *)d offset:(NSUInteger)o bytesPerRow:(NSUInteger)r {
    ++textures; assert(d && o==0 && d.storageMode==MTLStorageModeShared && d.usage==MTLTextureUsageShaderRead);
    if(failTexture) return nil;
    Texture *t=(Texture *)class_createInstance(objc_getClass("Texture"),0);
    t->buffer_=self; t->row_=r;t->width_=d->width_;t->height_=d->height_;return (id<MTLTexture>)t;
}
@end
@implementation Texture @end
@interface Blit : NSObject <MTLBlitCommandEncoder> @end
@implementation Blit
- (void)copyFromBuffer:(id<MTLBuffer>)s sourceOffset:(NSUInteger)o toBuffer:(id<MTLBuffer>)d destinationOffset:(NSUInteger)p size:(NSUInteger)n {
    Buffer *src=(Buffer *)s,*dst=(Buffer *)d;assert(o+n<=src->length_ && p+n<=dst->length_);memcpy(dst->bytes_+p,src->bytes_+o,n);++copies;
}
- (void)copyFromTexture:(id<MTLTexture>)s sourceSlice:(NSUInteger)i sourceLevel:(NSUInteger)l sourceOrigin:(MTLOrigin)o sourceSize:(MTLSize)z toBuffer:(id<MTLBuffer>)d destinationOffset:(NSUInteger)p destinationBytesPerRow:(NSUInteger)r destinationBytesPerImage:(NSUInteger)b {
    Texture *t=(Texture *)s;Buffer *dst=(Buffer *)d;assert(!i && !l && !o.x && !o.y && !o.z && z.depth==1 && z.width==t->width_ && z.height==t->height_ && b==r*z.height);
    for(NSUInteger y=0;y<z.height;++y) {assert(y*t->row_+z.width*4<=t->buffer_->length_ && p+y*r+z.width*4<=dst->length_);memcpy(dst->bytes_+p+y*r,t->buffer_->bytes_+y*t->row_,z.width*4);}++copies;
}
- (void)endEncoding {}
@end
@interface Commands : NSObject <MTLCommandBuffer> @end
@implementation Commands
- (id<MTLBlitCommandEncoder>)blitCommandEncoder {
    if(hook) { hook(hookContext,1); hook(hookContext,2); if(failBlit) hook(hookContext,17); }
    return failBlit ? nil : (id<MTLBlitCommandEncoder>)class_createInstance(objc_getClass("Blit"),0);
}
- (void)commit {}
- (void)waitUntilCompleted {}
- (NSUInteger)status {return failGpu ? 5 : MTLCommandBufferStatusCompleted;}
@end
@interface Queue : NSObject <MTLCommandQueue> @end
@implementation Queue
- (id<MTLCommandBuffer>)commandBuffer {return (id<MTLCommandBuffer>)class_createInstance(objc_getClass("Commands"),0);}
@end
@interface Device : NSObject <MTLDevice> @end
@implementation Device
- (id<MTLCommandQueue>)newCommandQueue {return (id<MTLCommandQueue>)class_createInstance(objc_getClass("Queue"),0);}
- (id<MTLBuffer>)newBufferWithBytesNoCopy:(void *)p length:(NSUInteger)n options:(NSUInteger)o deallocator:(void *)d {
    assert(o==MTLResourceStorageModeShared && !d);Buffer *b=(Buffer *)class_createInstance(objc_getClass("Buffer"),0);b->bytes_=(unsigned char *)p;b->length_=n;++sourceImports;return (id<MTLBuffer>)b;
}
- (NSUInteger)minimumLinearTextureAlignmentForPixelFormat:(NSUInteger)f {assert(f==MTLPixelFormatBGRA8Unorm);++alignments;return 16;}
@end
int main() {
    const size_t page=getpagesize();void *a=nullptr,*b=nullptr;assert(!posix_memalign(&a,page,page) && !posix_memalign(&b,page,page));
    auto *src=(unsigned char *)a,*dst=(unsigned char *)b;Device *dev=(Device *)class_createInstance(objc_getClass("Device"),0);
    gpupresent::Presenter p((id<MTLDevice>)dev,{dst,page,24,4,3});assert(p.ready());
    gpupresent::CopyTrace trace;trace.configure(testClock);trace.configureEncoder(install);
    assert(p.present({src,page,32,5,3},&trace)==gpupresent::kBadFrame);
    assert(trace.snapshot(ticks).phase==0 && sets==0);
    memset(src,0x35,page);assert(p.present({src,page,32,4,3},&trace)==gpupresent::kOk);
    auto snap=trace.snapshot(ticks);assert(snap.returned && snap.phase==5);
    assert((snap.encoderMask & ((1u<<0)|(1u<<1)|(1u<<23)|(1u<<24))) == ((1u<<0)|(1u<<1)|(1u<<23)|(1u<<24)));
    assert(!hook && !hookContext && sets==2);
    assert(p.present({src,page,32,4,3},&trace)==gpupresent::kOk && sets==2);
    gpupresent::CopyTrace bad;bad.configure(testClock);bad.configureEncoder(install);failBlit=true;
    assert(p.present({src,page,32,4,3},&bad)==gpupresent::kNoResource);
    auto failed=bad.snapshot(ticks);assert(failed.returned && (failed.encoderMask & (1u<<16)) && (failed.encoderMask & (1u<<24)));
    assert(!hook && !hookContext && sets==4);
    free(a);free(b);
}
'''
with tempfile.TemporaryDirectory() as d:
    tmp=Path(d);(tmp/'Metal').mkdir();(tmp/'Metal/Metal.h').write_text(metal);(tmp/'mock.mm').write_text(source)
    objc=subprocess.check_output(['gcc','-print-file-name=libobjc.so'],text=True).strip()
    if objc=='libobjc.so': raise SystemExit('GNU Objective-C runtime development library required')
    search=subprocess.run([os.environ.get('CXX','c++'),'-E','-v','-x','c++','-'],input='',text=True,capture_output=True,check=True).stderr
    includes=[]
    for line in search.split('#include <...> search starts here:')[1].split('End of search list.')[0].splitlines():
        if line.strip(): includes += ['-isystem',line.strip()]
    subprocess.run([os.environ.get('CLANGXX','clang++'),*includes,'-std=c++17','-x','objective-c++','-fobjc-runtime=gcc',
                    '-Wall','-Wextra','-Werror','-I',str(tmp),'-I',str(root),str(tmp/'mock.mm'),'-x','none',objc,
                    '-L'+str(Path(objc).parent),'-o',str(tmp/'test')],check=True)
    subprocess.run([str(tmp/'test')],check=True)
print('PASS: actual presenter invalid-frame retry, hook scope, nil encoder, first-copy only')
