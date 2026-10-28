#!/usr/bin/env python3
"""Execute production fixed queue under MPSC load and default-off frontend hooks."""
from pathlib import Path
import os,re,subprocess,tempfile
r=Path(__file__).resolve().parents[1]
s=(r/'NativeBridgeMetalFrontend.mm').read_text()
a=s.index('static std::atomic<bool> gUiLatencyEnabled');b=s.index('static std::mutex gCommitLock',a)
hooks=s[a:b]
source=r'''
#include "UiLatencyTrace.h"
#include <atomic>
#include <thread>
#include <cassert>
#include <vector>
static unsigned clocks;
static uint64_t mach_absolute_time(){++clocks;return 999;}
HOOKS
int main(){
uint64_t out[4];uiLatency(10,1,2);assert(!clocks&&!native_bridge_metal_latency_event(out));
native_bridge_metal_enable_latency(true);uiLatency(10,4,3,123);
assert(native_bridge_metal_latency_event(out)&&out[0]==123&&out[1]==4&&out[2]==10&&out[3]==3);
uiLatency(12,4,3);assert(clocks==1);assert(native_bridge_metal_latency_event(out)&&out[0]==999);
uilatency::Trace queue;for(unsigned i=0;i<256;i++)assert(queue.push({i,i,30,0}));
assert(!queue.push({1,1,30,0})&&queue.lost()==1);
uilatency::Event e;for(unsigned i=0;i<256;i++){assert(queue.pop(e));assert(e.id==i&&e.tick==i);}
assert(!queue.pop(e));
std::atomic<unsigned> done{0},accepted{0};std::vector<std::thread> writers;
for(unsigned j=0;j<4;j++)writers.emplace_back([&,j]{for(unsigned i=0;i<10000;i++){
const uint64_t id=uint64_t(j)*10000+i;if(queue.push({id+5,id,30,7}))++accepted;}++done;});
unsigned consumed=0;while(done<4){if(queue.pop(e)){assert(e.tick==e.id+5&&e.kind==30&&e.work==7);++consumed;}}
for(auto &t:writers){t.join();}
while(queue.pop(e))++consumed;
assert(consumed==accepted);assert(consumed+queue.lost()==40001);
}
'''.replace('HOOKS',hooks)
with tempfile.TemporaryDirectory() as d:
 p=Path(d);(p/'test.cpp').write_text(source)
 subprocess.run([os.environ.get('CXX','c++'),'-std=c++17','-pthread','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-I',str(r),str(p/'test.cpp'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)
thread_source=r'''
#define NMB_RUNTIME_COMMIT_TRACE 1
#define __APPLE__ 1
#include "UiLatencyTrace.h"
#include <atomic>
#include <cassert>
static unsigned tidCalls; static bool failTid;
static uint64_t mach_absolute_time(){return 101;}
static int pthread_threadid_np(void *,uint64_t *out){++tidCalls;*out=0x100000123ULL;return failTid?1:0;}
HOOKS
int main(){(void)runtimeTraceClock;uint64_t out[5];runtimeTraceSink(64,7,200);assert(!tidCalls&&!native_bridge_metal_latency_event_with_tid(out));
 native_bridge_metal_enable_latency(true);runtimeTraceSink(64,7,200);assert(tidCalls==1);
 assert(native_bridge_metal_latency_event_with_tid(out)&&out[0]==200&&out[1]==7&&out[2]==64&&out[4]==0x100000123ULL);
 runtimeTraceSink(60,8,201);assert(tidCalls==1);assert(native_bridge_metal_latency_event_with_tid(out)&&out[4]==0);
 failTid=true;runtimeTraceSink(65,9,202);assert(tidCalls==2);assert(native_bridge_metal_latency_event_with_tid(out)&&out[4]==0);
 assert(!native_bridge_metal_latency_event_with_tid(nullptr));}
'''.replace('HOOKS',hooks)
with tempfile.TemporaryDirectory() as d:
 p=Path(d);(p/'tid.cpp').write_text(thread_source)
 subprocess.run([os.environ.get('CXX','c++'),'-std=c++17','-pthread','-Wall','-Wextra','-Werror','-I',str(r),str(p/'tid.cpp'),'-o',str(p/'tid')],check=True)
 subprocess.run([str(p/'tid')],check=True)
assert 'PVR_METAL_LATENCY_DIAG' in (r/'NativeVulkanMetalPlugin.mm').read_text()
commit=s[s.index('- (void)commit {'):s.index('- (void)waitUntilCompleted {')]
assert 'nmb_command_buffer_commit_profiled(_handle,self.commitIndex,runtimeTraceClock,runtimeTraceSink)' in commit
assert '(gUiLatencyEnabled.load(std::memory_order_relaxed)\n        ? nmb_command_buffer_commit_profiled' in commit
assert 'encodedWork.passes && gUiLatencyEnabled' not in commit
commit=re.sub(r'#if defined\(NMB_RUNTIME_COMMIT_TRACE\)\n.*?#else\n(.*?)#endif\n',r'\1',commit,flags=re.S)
enter=commit.index('uiLatency(encodedWork.passes?10:20')
before=commit.index('if(encodedWork.passes)uiLatency(13')
flush=commit.index('flushSurfaces(_surfaces)')
after=commit.index('if(encodedWork.passes)uiLatency(14')
submit=commit.index('nmb_command_buffer_commit(_handle)')
end=commit.index('uiLatency(encodedWork.passes?12:22')
assert enter < before < flush < after < submit < end
start=commit.index('if(nmb_command_buffer_on_completed(')
finish=commit.index(') {\n   uiLatency(encodedWork.passes?12:22',start)
condition=commit[start+3:finish].replace('self.commitIndex','index')
exercise=r'''
#include "UiLatencyTrace.h"
#include <atomic>
#include <cassert>
static unsigned clocks;
static uint64_t mach_absolute_time(){return ++clocks;}
HOOKS
struct Work {unsigned passes,draws;} encodedWork;
static uint64_t index; static int _handle,context,completed,scheduled;
static int calls, failAt; static bool earlyCallback;
static bool call(int which){++calls;assert(calls==which);return which!=failAt;}
static bool nmb_command_buffer_on_completed(int,int,int){return call(1);}
static bool nmb_command_buffer_on_scheduled(int,int,int){return call(2);}
static bool nmb_command_buffer_commit(int){bool result=call(3);if(result && earlyCallback && encodedWork.passes)uiLatency(11,index,encodedWork.draws);return result;}
static bool run(){return CONDITION;}
int main(){
 for(int fail=0;fail<=3;++fail)for(int render=0;render<2;++render)for(int enabled=0;enabled<2;++enabled){
  calls=0;failAt=fail;clocks=0;encodedWork={unsigned(render),3};index=7;earlyCallback=true;
  native_bridge_metal_enable_latency(enabled);bool ok=run();assert(ok==(fail==0));assert(calls==(fail?fail:3));
  uint64_t out[4];unsigned kinds[3]={15,16,11};unsigned n=0;
  while(native_bridge_metal_latency_event(out)){assert(enabled&&render&&out[1]==7&&out[2]==kinds[n++]);}
  unsigned expected=enabled&&render?(fail==1?0:fail==2?1:fail==3?2:3):0;
  assert(n==expected);assert(clocks==expected);
 }
}
'''.replace('HOOKS',hooks).replace('CONDITION',condition)
with tempfile.TemporaryDirectory() as d:
 p=Path(d);(p/'condition.cpp').write_text(exercise)
 subprocess.run([os.environ.get('CXX','c++'),'-std=c++17','-pthread','-Wall','-Wextra','-Werror','-I',str(r),str(p/'condition.cpp'),'-o',str(p/'condition')],check=True)
 subprocess.run([str(p/'condition')],check=True)
print('PASS actual commit condition: off/non-render, registration failures, commit refusal and early callback')
print('PASS actual frontend hooks default-off/raw timestamps and bounded MPSC queue/drop accounting')
