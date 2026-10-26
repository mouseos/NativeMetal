#!/usr/bin/env python3
"""Compile the five actual frontend async methods/helpers, with controlled sync
compile boundaries, on macOS. No bridge/Rust build or GPU is needed. Also compile
full production frontend for iPhoneOS. --source-only checks extraction on Linux;
it does not claim execution. Supply --out outside the source repository.
"""
import argparse
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / 'NativeBridgeMetalFrontend.mm'


def extracted():
    text = SOURCE.read_text()
    begin = text.index('// BEGIN async creation dispatch')
    end = text.index('// END async creation dispatch', begin)
    helper = text[begin:end]
    methods = []
    for match in re.finditer(r'^- \(void\)[^{]+completionHandler:[^{]+\{\n.*?^}', text, re.M | re.S):
        head = match.group().split('{', 1)[0]
        if any(name in head for name in ('newRenderPipelineState', 'newComputePipelineState', 'newFunctionWithName')):
            body = match.group()
            assert body.count('enqueueCreation(') == 1 and body.count('completeCreation(') == 1
            assert 'error:&error' in body and 'gAsyncCreation.load' in body
            methods.append(body)
    assert len(methods) == 5
    assert 'gAsyncCreation{false}' in helper
    return helper, methods


PRE = r'''
#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#include <atomic>
#include <cassert>
#include <cstdio>
#include <initializer_list>
#include <mutex>
#include <string>
#include <dispatch/dispatch.h>
static void waitFor(dispatch_semaphore_t s) {
 assert(dispatch_semaphore_wait(s,dispatch_time(DISPATCH_TIME_NOW,10*NSEC_PER_SEC))==0);
}
@interface Token : NSObject
@property(nonatomic,copy) NSString *name;
@property(nonatomic,copy) NSString *compileName;
@end
@implementation Token
@end
// A mutable constants object with NSCopying, to observe the production copy
// boundary without relying on private MTLFunctionConstantValues readback SPI.
@interface Constants : NSObject <NSCopying>
@property(nonatomic) unsigned value;
@end
@implementation Constants
- (id)copyWithZone:(NSZone *)zone {(void)zone;Constants *v=[Constants new];v.value=self.value;return v;}
@end
static std::atomic<unsigned> calls,active,maximum,callbacks;
static bool blocked,wantFailure;
static MTLPipelineOption expectedOptions;
static dispatch_semaphore_t entered,releaseCompile;
static id backend(NSString *name,id argument,MTLPipelineOption options,NSError **error) {
 unsigned n=++calls,a=++active,seen=maximum.load();
 while(a>seen&&!maximum.compare_exchange_weak(seen,a)){}
 if(blocked&&n==1){dispatch_semaphore_signal(entered);waitFor(releaseCompile);}
 // Observe snapshots AFTER the caller has mutated and released original inputs.
 assert([name isEqualToString:n==1?(wantFailure?@"outer-fail":@"outer-ok"):@"nested-ok"]);
 if([argument isKindOfClass:[MTLRenderPipelineDescriptor class]]) {
  MTLRenderPipelineDescriptor *d=argument;
  assert(d.colorAttachments[0].pixelFormat==MTLPixelFormatBGRA8Unorm);
  assert([[(Token *)(id)d.vertexFunction name] isEqualToString:@"function-owner"]);
 } else if([argument isKindOfClass:[Constants class]]) assert([(Constants *)argument value]==42);
 else assert([[(Token *)argument name] isEqualToString:@"function-owner"]);
 assert(options==expectedOptions);
 --active;
 if(n==1&&wantFailure){if(error)*error=[NSError errorWithDomain:@"creation-fixture" code:17 userInfo:@{NSLocalizedDescriptionKey:name}];return nil;}
 if(error)*error=nil;Token *token=[Token new];token.name=name;return token;
}
@interface Device : NSObject
- (id<MTLRenderPipelineState>)newRenderPipelineStateWithDescriptor:(MTLRenderPipelineDescriptor *)d error:(NSError **)e;
- (id<MTLRenderPipelineState>)newRenderPipelineStateWithDescriptor:(MTLRenderPipelineDescriptor *)d options:(MTLPipelineOption)o reflection:(MTLAutoreleasedRenderPipelineReflection *)r error:(NSError **)e;
- (id<MTLComputePipelineState>)newComputePipelineStateWithFunction:(id<MTLFunction>)f error:(NSError **)e;
- (id<MTLComputePipelineState>)newComputePipelineStateWithFunction:(id<MTLFunction>)f options:(MTLPipelineOption)o reflection:(MTLAutoreleasedComputePipelineReflection *)r error:(NSError **)e;
DEVICE_DECLS
@end
@implementation Device
- (id<MTLRenderPipelineState>)newRenderPipelineStateWithDescriptor:(MTLRenderPipelineDescriptor *)d error:(NSError **)e {return (id)backend(d.label,d,0,e);}
- (id<MTLRenderPipelineState>)newRenderPipelineStateWithDescriptor:(MTLRenderPipelineDescriptor *)d options:(MTLPipelineOption)o reflection:(MTLAutoreleasedRenderPipelineReflection *)r error:(NSError **)e {if(r)*r=nil;return (id)backend(d.label,d,o,e);}
- (id<MTLComputePipelineState>)newComputePipelineStateWithFunction:(id<MTLFunction>)f error:(NSError **)e {return (id)backend([(Token *)(id)f compileName],f,0,e);}
- (id<MTLComputePipelineState>)newComputePipelineStateWithFunction:(id<MTLFunction>)f options:(MTLPipelineOption)o reflection:(MTLAutoreleasedComputePipelineReflection *)r error:(NSError **)e {if(r)*r=nil;return (id)backend([(Token *)(id)f compileName],f,o,e);}
DEVICE_METHODS
@end
@interface Library : NSObject
- (id<MTLFunction>)newFunctionWithName:(NSString *)n constantValues:(MTLFunctionConstantValues *)v error:(NSError **)e;
LIBRARY_DECLS
@end
@implementation Library
- (id<MTLFunction>)newFunctionWithName:(NSString *)n constantValues:(MTLFunctionConstantValues *)v error:(NSError **)e {return (id)backend(n,v,0,e);}
LIBRARY_METHODS
@end
'''
MAIN = r'''
static void issue(unsigned kind,id owner,MTLRenderPipelineDescriptor *d,Token *f,NSString *name,Constants *constants,void (^done)(id,NSError *)) {
 id<MTLDevice> dev=(id)owner;
 switch(kind){
 case 0:{[dev newRenderPipelineStateWithDescriptor:d completionHandler:^(id s,NSError *e){done(s,e);}];break;}
 case 1:{[dev newRenderPipelineStateWithDescriptor:d options:MTLPipelineOptionArgumentInfo completionHandler:^(id s,MTLRenderPipelineReflection *r,NSError *e){assert(!r);done(s,e);}];break;}
 case 2:{[dev newComputePipelineStateWithFunction:(id)f completionHandler:^(id s,NSError *e){done(s,e);}];break;}
 case 3:{[dev newComputePipelineStateWithFunction:(id)f options:MTLPipelineOptionArgumentInfo completionHandler:^(id s,MTLComputePipelineReflection *r,NSError *e){assert(!r);done(s,e);}];break;}
 case 4:{[(Library *)owner newFunctionWithName:name constantValues:(id)constants completionHandler:^(id s,NSError *e){done(s,e);}];break;}
 default:assert(false);
 }
}
static Token *function(NSString *compileName){Token *f=[Token new];f.name=@"function-owner";f.compileName=compileName;return f;}
static MTLRenderPipelineDescriptor *descriptor(Token *f,NSString *name){
 MTLRenderPipelineDescriptor *d=[MTLRenderPipelineDescriptor new];d.label=name;d.vertexFunction=(id)f;d.colorAttachments[0].pixelFormat=MTLPixelFormatBGRA8Unorm;return d;
}
static Constants *constants(){Constants *v=[Constants new];v.value=42;return v;}
static void checkResult(id state,NSError *error,bool failure){
 assert(bool(state)==!failure&&bool(error)==failure);
 if(failure)assert(error.code==17&&[error.localizedDescription isEqualToString:@"outer-fail"]);
}
static void run(unsigned kind,bool failure,bool async){
 calls=active=maximum=callbacks=0;blocked=async;wantFailure=failure;
 expectedOptions=(kind==1||kind==3)?MTLPipelineOptionArgumentInfo:MTLPipelineOptionNone;
 entered=dispatch_semaphore_create(0);releaseCompile=dispatch_semaphore_create(0);
 NSString *label=failure?@"outer-fail":@"outer-ok";
 native_bridge_metal_set_async_creation(async);
 if(!async){
  id owner=kind==4?(id)[Library new]:(id)[Device new];Token *f=function(label);
  __block bool inside=true;
  issue(kind,owner,descriptor(f,label),f,label,constants(),^(id state,NSError *error){assert(inside);checkResult(state,error,failure);++callbacks;});
  inside=false;assert(calls==1&&callbacks==1);return;
 }
 dispatch_semaphore_t returned=dispatch_semaphore_create(0),done=dispatch_semaphore_create(0);
 NSLock *callerLock=[NSLock new];
 __block __weak id weakOwner=nil;__block __weak Token *weakFunction=nil;
 dispatch_async(dispatch_get_global_queue(QOS_CLASS_DEFAULT,0),^{
  @autoreleasepool {
   id owner=kind==4?(id)[Library new]:(id)[Device new];Token *f=function(label);
   weakOwner=owner;weakFunction=f;
   MTLRenderPipelineDescriptor *d=descriptor(f,label);NSMutableString *name=[label mutableCopy];Constants *v=constants();
   [callerLock lock];
   issue(kind,owner,d,f,name,v,^(id state,NSError *error){
    // Under the old inline implementation this blocks on the caller's own lock.
    [callerLock lock];checkResult(state,error,failure);[callerLock unlock];
    dispatch_semaphore_t nested=dispatch_semaphore_create(0);
    id nestedOwner=kind==4?(id)[Library new]:(id)[Device new];Token *nf=function(@"nested-ok");
    issue(kind,nestedOwner,descriptor(nf,@"nested-ok"),nf,@"nested-ok",constants(),^(id ns,NSError *ne){assert(ns&&!ne);dispatch_semaphore_signal(nested);});
    // Fails if callback occupies serial compilation OR serial completion queue.
    waitFor(nested);++callbacks;dispatch_semaphore_signal(done);
   });
   d.label=@"mutated";d.colorAttachments[0].pixelFormat=MTLPixelFormatInvalid;d.vertexFunction=nil;
   [name setString:@"mutated"];v.value=999;
   [callerLock unlock];
   owner=nil;f=nil;d=nil;name=nil;v=nil;
  }
  dispatch_semaphore_signal(returned);
 });
 waitFor(entered);waitFor(returned); // compile still blocked: caller/pool already returned
 assert(weakOwner);if(kind!=4)assert(weakFunction);
 dispatch_semaphore_signal(releaseCompile);
 waitFor(done);assert(calls==2&&callbacks==1&&active==0&&maximum==1);
}
int main(){@autoreleasepool {
 // Also verify the untouched default; no setter is called before this assertion.
 assert(!gAsyncCreation.load());
 for(unsigned kind=0;kind<5;kind++)for(bool failure:{false,true})for(bool async:{false,true})run(kind,failure,async);
 puts("PASS actual five creation methods: default inline, opt-in nonblocking, caller lock reentry, nested callbacks, copied descriptor/constants/name, retained owner/function, success+failure exactly once, nil reflection, serial compile bound");
}}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--source-only', action='store_true')
    args = parser.parse_args()
    helper, methods = extracted()
    library = [m for m in methods if 'newFunctionWithName:' in m.split('{', 1)[0]]
    device = [m for m in methods if m not in library]
    fixture = PRE
    for name, group in [('DEVICE', device), ('LIBRARY', library)]:
        fixture = fixture.replace(name + '_DECLS', '\n'.join(m.split('{', 1)[0] + ';' for m in group))
        fixture = fixture.replace(name + '_METHODS', '\n'.join(group))
    fixture = fixture.replace('@interface Token', helper + '\n@interface Token', 1) + MAIN
    if args.out:
        args.out.mkdir(parents=True, exist_ok=True)
        source = args.out / 'async_creation_actual.mm'
        source.write_text(fixture)
    if args.source_only:
        print('PASS extraction of five actual production methods and dispatch helpers; macOS execution NOT performed')
        return
    if sys.platform != 'darwin' or not args.out:
        parser.error('macOS and --out are required for execution (or use --source-only)')
    binary = args.out / 'async_creation_actual'
    flags = ['-std=c++17', '-fobjc-arc', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-Wno-unused-parameter', '-Wno-deprecated-declarations']
    subprocess.run(['xcrun', 'clang++', *flags, str(source), '-framework', 'Foundation', '-framework', 'Metal', '-o', str(binary)], check=True, timeout=120)
    subprocess.run([str(binary)], check=True, timeout=90)
    sdk = subprocess.check_output(['xcrun', '--sdk', 'iphoneos', '--show-sdk-path'], text=True).strip()
    subprocess.run(['xcrun', 'clang++', *flags, '-target', 'arm64-apple-ios17.0', '-isysroot', sdk,
                    '-I', str(ROOT), '-c', str(SOURCE), '-o', str(args.out / 'NativeBridgeMetalFrontend-ios.o')], check=True, timeout=120)
    print('PASS full production frontend iPhoneOS compile; no Rust rebuild/GPU execution')


if __name__ == '__main__':
    main()
