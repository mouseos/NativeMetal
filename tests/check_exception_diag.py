#!/usr/bin/env python3
"""実formatterをASan/UBSanで実行し、ヘッダ全体をARC構文検証。Apple SDK実行の代替ではない。"""
from pathlib import Path
import shutil
import subprocess
import tempfile
ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / 'ExceptionDiag.h').read_text()
foundation = r'''

#pragma once
#include <stddef.h>
typedef unsigned long NSUInteger;
typedef unsigned short unichar;
struct NSRange {NSUInteger location,length;};
static inline NSRange NSMakeRange(NSUInteger a,NSUInteger b){return {a,b};}
__attribute__((objc_root_class)) @interface NSObject { @public void *isa; }
+ (instancetype)new;
@end
@interface NSString : NSObject { @public char *bytes; unsigned count; }
@property(readonly) NSUInteger length;
@property(readonly) const char *UTF8String;
- (NSString *)substringToIndex:(NSUInteger)n;
- (unichar)characterAtIndex:(NSUInteger)n;
+ (instancetype)fromC:(const char *)text;
@end
@interface NXConstantString : NSString @end
@interface NSMutableString : NSString
+ (instancetype)stringWithFormat:(NSString *)format,...;
- (void)appendString:(NSString *)text;
- (void)appendFormat:(NSString *)format,...;
- (void)replaceCharactersInRange:(NSRange)range withString:(NSString *)text;
@end
@interface NSNumber : NSObject { @public unsigned long long value; }
@property(readonly) unsigned long long unsignedLongLongValue;
@end
@interface NSArray<__covariant ObjectType> : NSObject { @public id values[24]; NSUInteger size; }
@property(readonly) NSUInteger count;
- (ObjectType)objectAtIndex:(NSUInteger)index;
@end
@interface NSException : NSObject { @public NSString *exceptionName,*exceptionReason; NSArray<NSNumber *> *frames; }
@property(readonly) NSString *name;
@property(readonly) NSString *reason;
@property(readonly) NSArray<NSNumber *> *callStackReturnAddresses;
@end
@interface NSAutoreleasePool : NSObject
- (void)drain;
@end
typedef void NSUncaughtExceptionHandler(NSException *);
NSUncaughtExceptionHandler *NSGetUncaughtExceptionHandler(void);
void NSSetUncaughtExceptionHandler(NSUncaughtExceptionHandler *);
'''
# Only Objective-C exception syntax is adapted; the production formatter body,
# limits, atomic claim and C++ sink catch are compiled verbatim.
start=SOURCE.index('using Output =')
end=SOURCE.index('static void handler(')
formatter=SOURCE[start:end].replace('@try {','try {').replace('@catch (NSException *)','catch (const ObjCFailure &)')
fixture=r'''
#include <atomic>
#include <cassert>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include <unistd.h>
struct ObjCFailure {};
namespace exceptiondiag {
'''+formatter+r'''
}
static std::mutex gate;
static std::vector<std::string> lines;
static bool sinkThrows,reenter;
void sink(const char*s){
 assert(strlen(s)<=63);
 for(const unsigned char*p=(const unsigned char*)s;*p;++p)assert(*p>=32&&*p<127);
 if(reenter)exceptiondiag::emitText("recursive");
 if(sinkThrows)throw 7;
 std::lock_guard<std::mutex> guard(gate);lines.emplace_back(s);
}
int main(){
 using namespace exceptiondiag;
 emitText("off");assert(lines.empty()&&gDirectReported==0);
 setOutput(sink);emitText(nullptr);assert(lines.size()==1&&lines[0].find("format unavailable")!=std::string::npos);
 for(size_t size:{size_t(0),size_t(1),size_t(40),size_t(41),size_t(640),size_t(641),size_t(10000)}){
  gDirectReported=0;lines.clear();std::string text(size,'x');emitText(text.c_str());
  assert(lines.size()==(size?((size<640?size:640)+39)/40:1));
  assert((lines.back().find('+')!=std::string::npos)==(size>640));
  size_t before=lines.size();emitText("second");assert(lines.size()==before);
 }
 gDirectReported=0;lines.clear();emitText("a\n\t\x7f\xff");assert(lines[0].find("a????")!=std::string::npos);
 gDirectReported=0;lines.clear();reenter=true;emitText("outer");assert(lines.size()==1);reenter=false;
 gDirectReported=0;lines.clear();sinkThrows=true;emitText("throw");assert(lines.empty());sinkThrows=false;
 gDirectReported=0;lines.clear();std::string text(640,'z');std::vector<std::thread> workers;
 for(unsigned i=0;i<8;i++)workers.emplace_back([&]{emitText(text.c_str());});
 for(auto&t:workers){t.join();}assert(lines.size()==16);
 setOutput(nullptr);emitText("disabled");assert(lines.size()==16);
 puts("PASS actual formatter: default/off/on, null/empty, 40/640 boundaries, truncation, ASCII sanitization, once-only, recursive/throwing sink, 8 concurrent callers; ASan/UBSan");
}
'''
plugin=(ROOT/'NativeVulkanMetalPlugin.mm').read_text()
assert 'exceptiondiag::setOutput(output);' in plugin
assert 'static Output gOutput;' in SOURCE
handler=SOURCE[SOURCE.index('static void handler('):SOURCE.index('// Every process')]
assert handler.index('if (gHandling) return;')<handler.index('gHandling = true;')
assert '__atomic_store_n(&gText, captured, __ATOMIC_RELEASE);' in handler
assert 'if (gPrevious) gPrevious(exception);' in handler and '@finally' in handler
assert handler.index('if (gPrevious)')>handler.index('catch (...)')
assert '[frames objectAtIndex:i].unsignedLongLongValue' in handler and 'i < 20' in handler
board=ROOT.parents[2]/'UniXNU-a04br3/package/powervr/user/NativePowerVRMetalBoard.mm'
if board.is_file():
 b=board.read_text();gate=b.index('if(pipelineDiag && !strcmp(pipelineDiag,"1")) {')
 assert gate<b.index('native_metal_set_exception_output(native_metal_board_text);')<b.index('native_bridge_metal_set_pipeline_failure_callback(pipelineFailure);')
with tempfile.TemporaryDirectory(prefix='metal-exception-') as tmp:
 p=Path(tmp);(p/'Foundation').mkdir();(p/'Foundation/Foundation.h').write_text(foundation)
 (p/'syntax.mm').write_text('#include "ExceptionDiag.h"\nvoid use(){exceptiondiag::start();exceptiondiag::setOutput(nullptr);}\n')
 clang=shutil.which('clang++')
 if not clang:raise SystemExit('clang++ unavailable')
 args=[clang,'-std=c++17','-fexceptions','-fobjc-exceptions','-Werror','-I'+str(p),'-I'+str(ROOT)]
 gcc=Path('/usr/lib/gcc/x86_64-linux-gnu/13')
 if gcc.is_dir():args.insert(1,'--gcc-install-dir='+str(gcc))
 subprocess.run(args+['-fobjc-runtime=macosx-10.13','-fobjc-arc','-fsyntax-only',str(p/'syntax.mm')],check=True)
 print('PASS whole production header ARC syntax (minimal Foundation declarations; not Apple SDK)')
 (p/'fixture.cpp').write_text(fixture)
 subprocess.run(['c++','-std=c++17','-pthread','-Wall','-Wextra','-Werror','-Wno-unused-variable',
  '-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie','-g',str(p/'fixture.cpp'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True,timeout=10)
print('PASS integration source assertions; actual Objective-C handler execution and full plugin require Apple SDK/macOS')
