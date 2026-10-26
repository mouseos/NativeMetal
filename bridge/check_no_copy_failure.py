#!/usr/bin/env python3
"""Actual no-copy callback and optional board serializer; no GPU or BO access.
--objc also compiles/runs the actual raw Objective-C++ method with Foundation,
Metal headers, a mocked NMB runtime and ASan/UBSan on macOS.
"""
from pathlib import Path
import argparse, os, subprocess, tempfile
M = Path(__file__).resolve().parents[1]
p = argparse.ArgumentParser(description=__doc__)
p.add_argument('--board', type=Path)
p.add_argument('--objc', action='store_true')
a = p.parse_args()
front = (M/'NativeBridgeMetalFrontend.mm').read_text()
header = (M/'NativeBridgeMetalFrontend.h').read_text()
def function(text, signature):
    start=text.index(signature); at=text.index('{',start); end=at+1; depth=1
    while depth:
        depth+=(text[end]=='{')-(text[end]=='}'); end+=1
    return text[start:end]
typedef=next(l for l in header.splitlines() if l.startswith('typedef void (*NativeMetalNoCopyFailureCallback)'))
helper=front.split('// BEGIN raw no-copy refusal diagnostic')[1].split('// END raw no-copy refusal diagnostic')[0]
helper=helper[helper.index('\n')+1:]
method=function(front,'- (id<MTLBuffer>)newBufferWithBytesNoCopy:')
# Keep the source ordering and successful ownership transfer under test.
assert method.index('NSString *reason=lastError();') < method.index('(__bridge_transfer id)context') < method.index('NBLOG(') < method.index('reportNoCopyFailure(')
assert 'lastError()' not in method[method.index('NBLOG('):]
assert 'lock_guard' not in method and 'gSurfaceImportLock' not in method
assert method.count('reportNoCopyFailure(')==1
assert method.index('reportNoCopyFailure(')<method.index('gNoCopyAll++')
assert 'context?releaseBlock:nullptr,context' in method
assert 'thread_local!' in (M/'bridge/NativeMetalObjects.rs').read_text()
h=r'''
#include <atomic>
#include <cassert>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
''' + typedef+'\n'+helper+r'''
static std::mutex rowsLock;
static std::vector<std::string> rows;
static std::atomic<unsigned> callbacks{0}, sequenceMask{0};
static void capture(unsigned n,uintptr_t pointer,uint64_t length,uint64_t options,const char *error) {
 assert(pointer>=0x1000 && length==0xf000 && options==0x123456789abcdef0ULL);
 assert(strncmp(error,"thread",6)==0);
 callbacks++;sequenceMask.fetch_or(1u<<n);
 // Re-entry is permitted; replacing the callback must not deadlock or rearm.
 native_bridge_metal_set_no_copy_failure_callback(capture);
}
'''
if a.board:
    board=a.board.read_text()
    serializer=function(board,'void noCopyFailure(')
    gate=board[board.index('if(pipelineDiag && !strcmp(pipelineDiag,"1")) {'):board.index('// Mesa\'s PowerVR driver')]
    assert 'native_bridge_metal_set_no_copy_failure_callback(noCopyFailure);' in gate
    h+=r'''
static void native_metal_board_text(const char *s) {
 assert(strlen(s)<=62);std::lock_guard<std::mutex> l(rowsLock);rows.emplace_back(s);
}
'''+serializer+'\n'
h+=r'''
int main() {
 reportNoCopyFailure(reinterpret_cast<void *>(1),2,3,"disabled");
 assert(!gNoCopyFailureReports && !callbacks);
 native_bridge_metal_set_no_copy_failure_callback(capture);
 std::vector<std::thread> workers;
 for(unsigned i=0;i<32;i++)workers.emplace_back([i]{
  std::string error="thread"+std::to_string(i);
  reportNoCopyFailure(reinterpret_cast<void *>(uintptr_t(0x1000+i)),0xf000,0x123456789abcdef0ULL,error.c_str());
 });
 for(auto &t:workers)t.join();
 assert(callbacks==4 && sequenceMask==30 && gNoCopyFailureReports==4);
 native_bridge_metal_set_no_copy_failure_callback(nullptr);
 reportNoCopyFailure(nullptr,0,0,nullptr);assert(gNoCopyFailureReports==4);
 native_bridge_metal_set_no_copy_failure_callback(capture);
 reportNoCopyFailure(nullptr,0,0,nullptr);assert(callbacks==4);
'''
if a.board:
    h+=r'''
 noCopyFailure(1,UINTPTR_MAX,UINT64_MAX,UINT64_MAX,"original thread-local reason");
 assert(rows[0]=="NC1 P ffffffffffffffff");
 assert(rows[1]=="NC1 L ffffffffffffffff O ffffffffffffffff");
 assert(rows[2]=="NC1 E00 original thread-local reason");
 for(unsigned length:{0u,47u,48u,49u,511u,512u,513u,800u}) {
  rows.clear();std::string text(length,'E');noCopyFailure(4,0x105fdf000,0xf000,0,text.c_str());
  std::string joined;bool truncated=false;
  for(auto &row:rows) {
   if(row.find("truncated")!=std::string::npos)truncated=true;
   else if(row.rfind("NC4 E",0)==0)joined+=row.substr(8);
  }
  assert(joined==text.substr(0,512));assert(truncated==(length>512));assert(rows.size()<=14);
 }
 rows.clear();noCopyFailure(2,0,0,0,nullptr);assert(rows[2]=="NC2 E00 <null>");
'''
h+='}\n'
with tempfile.TemporaryDirectory(prefix='metal-no-copy-failure-') as tmp:
    source=Path(tmp)/'callback.cpp';source.write_text(h);binary=source.with_suffix('')
    subprocess.run([os.environ.get('CXX','c++'),'-std=c++17','-Wall','-Wextra','-Werror','-pthread',
                    '-fsanitize=address,undefined','-fno-omit-frame-pointer',str(source),'-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True,timeout=30)
    print('PASS actual callback: default null, concurrent first-four bound, no rearm, reentry; raw source error capture/cleanup ordering')
    if a.board: print('PASS actual board serializer: exact 64-bit metadata, <=62 chars/line, 512-byte error bound, boundary/truncation cases')
    if a.objc:
        objc=r'''
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <atomic>
#include <cassert>
#include <cstdint>
#include <mutex>
#include <string>
struct NmbBuffer {void (*release)(void *,void *,uint64_t);void *context,*pointer;uint64_t length;};
static void *gDevice;
static bool refuse;
static thread_local std::string runtimeError;
static unsigned deallocators,diagnostics;
static std::string captured;
static std::mutex gProblemLock,gSurfaceImportLock;
static std::atomic<uint64_t> gNoCopyAll,gNoCopyMade,gNoCopyReleased;
static NSString *lastError(){return [NSString stringWithUTF8String:runtimeError.c_str()];}
static NmbBuffer *nmb_buffer_new_no_copy_releasing(void *,void *p,uint64_t n,uint64_t,
                       void (*release)(void *,void *,uint64_t),void *context){
 if(refuse){runtimeError="original no-copy failure";return nullptr;}
 return new NmbBuffer{release,context,p,n};
}
static void nmb_buffer_release(NmbBuffer *b){if(b){if(b->release)b->release(b->context,b->pointer,b->length);delete b;}}
static void history(NSString *){std::lock_guard<std::mutex> l(gProblemLock);runtimeError="history replaced runtime error";}
#define NBLOG(...) history([NSString stringWithFormat:__VA_ARGS__])
'''+typedef+'\n'+helper+function(front,'static void releaseBlock(')+r'''
@interface NBBuffer:NSObject
@property NmbBuffer *handle;
@property MTLResourceOptions options;
@property void *wrapped;
@end
@implementation NBBuffer
-(void)dealloc{nmb_buffer_release(_handle);}
@end
@interface RawDevice:NSObject
'''+method[:method.index('{')]+''';
@end
@implementation RawDevice
'''+method+r'''
@end
static void failure(unsigned n,uintptr_t p,uint64_t length,uint64_t options,const char *error){
 assert(n==1 && p==0x105fdf000 && length==0xf000 && options==0);
 assert(gProblemLock.try_lock());gProblemLock.unlock();
 assert(gSurfaceImportLock.try_lock());gSurfaceImportLock.unlock();
 captured=error;diagnostics++;runtimeError="callback reentered";
}
int main(){@autoreleasepool{
 RawDevice *device=[RawDevice new];
 native_bridge_metal_set_no_copy_failure_callback(failure);
 refuse=true;
 __weak NSObject *failedOwner;
 @autoreleasepool {
  NSObject *owner=[NSObject new];failedOwner=owner;
  void (^block)(void *,NSUInteger)=^(void *,NSUInteger){assert(owner);deallocators++;};
  assert(![device newBufferWithBytesNoCopy:(void *)0x105fdf000 length:0xf000 options:0 deallocator:block]);
  owner=nil;block=nil;
 }
 assert(!failedOwner);
 assert(diagnostics==1 && captured=="original no-copy failure" && !deallocators && !gNoCopyAll && !gNoCopyReleased);
 refuse=false;
 __weak NSObject *successfulOwner;id<MTLBuffer> buffer;
 @autoreleasepool {
  NSObject *owner=[NSObject new];successfulOwner=owner;
  void (^block)(void *,NSUInteger)=^(void *p,NSUInteger length){assert(owner && p==(void *)0x105fdf000 && length==0xf000);deallocators++;};
  buffer=[device newBufferWithBytesNoCopy:(void *)0x105fdf000 length:0xf000 options:0 deallocator:block];
  owner=nil;block=nil;
  assert(buffer && !deallocators && diagnostics==1 && gNoCopyAll==1 && gNoCopyMade==1);
 }
 assert(successfulOwner);
 @autoreleasepool {buffer=nil;}
 assert(!successfulOwner);
 assert(deallocators==1 && gNoCopyReleased==1 && diagnostics==1);
}}
'''
        source=Path(tmp)/'raw.mm';source.write_text(objc);binary=source.with_suffix('')
        subprocess.run(['xcrun','clang++','-std=c++17','-fobjc-arc','-fblocks','-Wall','-Wextra','-Werror',
                        '-fsanitize=address,undefined',str(source),'-framework','Foundation','-framework','Metal','-o',str(binary)],check=True)
        subprocess.run([str(binary)],check=True,timeout=30)
        print('PASS actual Objective-C++ raw method: original TLS error preserved across history reentry; callback outside locks; failed/successful deallocator ownership')
