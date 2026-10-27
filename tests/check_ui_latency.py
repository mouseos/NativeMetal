#!/usr/bin/env python3
"""Execute production fixed queue under MPSC load and default-off frontend hooks."""
from pathlib import Path
import os,subprocess,tempfile
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
assert 'PVR_METAL_LATENCY_DIAG' in (r/'NativeVulkanMetalPlugin.mm').read_text()
commit=s[s.index('- (void)commit {'):s.index('- (void)waitUntilCompleted {')]
enter=commit.index('uiLatency(encodedWork.passes?10:20')
before=commit.index('if(encodedWork.passes)uiLatency(13')
flush=commit.index('flushSurfaces(_surfaces)')
after=commit.index('if(encodedWork.passes)uiLatency(14')
submit=commit.index('nmb_command_buffer_commit(_handle)')
end=commit.index('uiLatency(encodedWork.passes?12:22')
assert enter < before < flush < after < submit < end
print('PASS actual frontend hooks default-off/raw timestamps and bounded MPSC queue/drop accounting')
