#!/usr/bin/env python3
"""Check production fixed records and the patched runtime's per-thread hook without a GPU."""
import argparse
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--translator', type=Path, required=True, help='isolated pinned translator with 0009 applied')
args = parser.parse_args()
translator = args.translator.resolve()
module = translator/'crates/metal2vulkan-runtime/src/encoder_open_trace.rs'
assert module.is_file()
revision=subprocess.check_output(['git','-C',str(translator),'rev-parse','HEAD'],text=True).strip()
assert revision=='720ef1ce9ae2a2068999a8675717418b9416283e', 'translator revision mismatch'
patch=root/'bridge/patches/0009-encoder-open-trace.patch'
paths=['command_buffer/mod.rs','encoder.rs','encoder_open_trace.rs','lib.rs','spares.rs']
with tempfile.TemporaryDirectory() as d:
    base=Path(d)/'crates/metal2vulkan-runtime/src'
    for name in paths:
        if name=='encoder_open_trace.rs': continue
        target=base/name; target.parent.mkdir(parents=True,exist_ok=True)
        target.write_bytes(subprocess.check_output(['git','-C',str(translator),'show',f'HEAD:crates/metal2vulkan-runtime/src/{name}']))
    subprocess.run(['git','apply','--check',str(patch)],cwd=d,check=True)
    subprocess.run(['git','apply',str(patch)],cwd=d,check=True)
    for name in paths:
        assert (base/name).read_bytes()==(translator/'crates/metal2vulkan-runtime/src'/name).read_bytes(), name
cpp = r'''
#undef NDEBUG
#include <thread>
#include <atomic>
#include "ScanoutCopyTrace.h"
#include <cassert>
static uint64_t clockCount=0, now=100;
static uint64_t clockNow() {++clockCount; return now;}
using Callback = void (*)(void *, uint32_t);
static Callback active=nullptr; static void *ctx=nullptr; static unsigned setCalls=0;
static void set(Callback f,void *p) {active=f;ctx=p;++setCalls;}
int main() {
    gpupresent::CopyTrace off; off.configureEncoder(set);
    assert(!off.begin()); assert(clockCount==0 && setCalls==0);
    gpupresent::CopyTrace trace; trace.configure(clockNow);trace.configureEncoder(set);
    assert(trace.begin(800,1280,3328,3200)); trace.reached(2);trace.encoded(1);
    trace.encoderEnter();assert(active && ctx==&trace && setCalls==1);
    active(ctx,0);active(ctx,26);assert(trace.snapshot(now).encoderMask==(1u<<23));
    for(unsigned e: {1u,2u,3u,4u,6u,7u,8u,11u,14u,15u,22u}) {now+=100; active(ctx,e);}
    auto x=trace.snapshot(now);
    assert(x.encoderMask == ((1u<<0)|(1u<<1)|(1u<<2)|(1u<<3)|(1u<<5)|(1u<<6)|(1u<<7)|(1u<<10)|(1u<<13)|(1u<<14)|(1u<<21)|(1u<<23)));
    assert(x.encoderTimes[1]-x.encoderTimes[0]==100 && x.encoderTimes[3]-x.encoderTimes[2]==100);
    assert(x.encoderTimes[7]-x.encoderTimes[6]==100 && x.encoderTimes[14]-x.encoderTimes[13]==100);
    assert(x.encoderTimes[8]==0 && x.encoderTimes[11]==0);
    assert(gpupresent::CopyTrace::encoderIntervalUs(x,1,2,now)==100);
    assert(gpupresent::CopyTrace::encoderIntervalUs(x,9,10,now)==UINT32_MAX);
    assert(gpupresent::CopyTrace::encoderIntervalUs(x,1,25,now)==now-x.encoderTimes[0]);
    assert(gpupresent::CopyTrace::encoderIntervalUs(x,1,25,x.encoderTimes[0]-1)==UINT32_MAX);
    const uint64_t before=x.encoderTimes[0];now+=500;active(ctx,1);assert(trace.snapshot(now).encoderTimes[0]==before);
    trace.encoderLeave();assert(!active && !ctx && setCalls==2);
    trace.encoded(2);trace.reached(3);trace.returned();
    auto stable=trace.snapshot(now+10000);assert(stable.encoderMask==(x.encoderMask | (1u<<24)));
    assert(gpupresent::CopyTrace::encoderIntervalUs(stable,24,25,now+10000)==now-stable.encoderTimes[23]);
    assert(!trace.begin(1,1,1,1));
    gpupresent::CopyTrace concurrent;concurrent.configure(clockNow);concurrent.configureEncoder(set);
    assert(concurrent.begin());concurrent.reached(2);concurrent.encoderEnter();
    Callback writer=active;void *writerCtx=ctx;std::atomic<bool> done{false};
    std::thread t([&]{for(unsigned i=1;i<=23;++i) {now+=10;writer(writerCtx,i);} done.store(true);});
    while(!done.load()) {auto snap=concurrent.snapshot(1000000000);for(unsigned i=0;i<23;++i)
        if(snap.encoderMask & (1u<<i)) assert(snap.encoderTimes[i] != 0);}
    t.join();concurrent.encoderLeave();auto all=concurrent.snapshot(now);
    assert((all.encoderMask & ((1u<<25)-1)) == ((1u<<25)-1));
}
'''
rust = r'''
#[path = "MODULE"] mod encoder_open_trace;
use std::ffi::c_void;
use std::sync::{Arc, Barrier, Mutex};
unsafe extern "C" fn record(p:*mut c_void, e:u32) {
    unsafe { (&*(p as *const Mutex<Vec<u32>>)).lock().unwrap().push(e); }
}
#[test]
fn disabled_and_scoped_and_threads() {
    let values=Arc::new(Mutex::new(Vec::<u32>::new()));
    encoder_open_trace::mark(1); assert!(values.lock().unwrap().is_empty());
    let ready=Arc::new(Barrier::new(2)); let v=values.clone(); let r=ready.clone();
    let t=std::thread::spawn(move || {
        unsafe {encoder_open_trace::set_encoder_open_callback(Some(record),Arc::as_ptr(&v) as *mut c_void)};
        encoder_open_trace::mark(1); r.wait(); r.wait(); encoder_open_trace::mark(2);
        unsafe {encoder_open_trace::set_encoder_open_callback(None,std::ptr::null_mut())};
        encoder_open_trace::mark(3);
    });
    ready.wait();encoder_open_trace::mark(9);ready.wait();t.join().unwrap();
    assert_eq!(*values.lock().unwrap(),vec![1,2]);
}
'''.replace('MODULE',str(module))
with tempfile.TemporaryDirectory() as d:
    tmp=Path(d)
    (tmp/'test.cpp').write_text('#include <initializer_list>\n'+cpp)
    subprocess.run(['c++','-std=c++17','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-fno-omit-frame-pointer',
                    '-I',str(root),str(tmp/'test.cpp'),'-o',str(tmp/'test')],check=True)
    subprocess.run([str(tmp/'test')],check=True)
    (tmp/'test.rs').write_text(rust)
    subprocess.run(['rustc','+1.98.0','--edition=2024','--test',str(tmp/'test.rs'),'-o',str(tmp/'rust-test')],check=True)
    subprocess.run([str(tmp/'rust-test')],check=True)
# Event delimiters are checked in the real patched functions, not only the callback.
checks={
'encoder.rs':['mark(1);','let mut state = command_buffer.state();','mark(2);','mark(14);','device.begin_command_buffer(raw, &begin)','mark(15);','mark(21);','mark(22);'],
'spares.rs':['mark(3);','lock(&self.command_pools)','mark(4);','take_command_pool(&mut pools, family)','mark(5);','mark(6);','mark(7);','reset_command_pool(pool.raw','mark(8);','mark(18);'],
'command_buffer/mod.rs':['mark(23);','spares\n                .command_pool','mark(9);','raw.create_command_pool(&info, None)','mark(10);','mark(19);','pool.reuse(level)','mark(11);','mark(12);','device.allocate_command_buffers(&info)','mark(13);','mark(20);'],
}
base=translator/'crates/metal2vulkan-runtime/src'
for path, needles in checks.items():
    data=(base/path).read_text(); start=data.index('pub(super) fn open(') if path=='encoder.rs' else data.index('pub(super) fn command_pool(') if path=='spares.rs' else data.index('    fn allocate(\n')
    cursor=start
    for needle in needles:
        cursor=data.index(needle,cursor)+len(needle)
assert 'EncoderTraceScope encoderTrace(tracing ? trace : nullptr);' in (root/'GpuScanoutPresenter.h').read_text()
plugin=(root/'NativeVulkanMetalPlugin.mm').read_text()
assert 'gCopyTrace.configureEncoder(nmb_encoder_open_trace_set)' in plugin
assert plugin.index('gCopyTrace.snapshot(encoderNow)') < plugin.index('const uint64_t intervalNow = copyClockUs()') < plugin.index('encoderIntervalUs(copy, a, b, intervalNow)')
print('PASS: scoped first-copy records, bounded events, no-op default, Rust thread isolation, production boundaries')
