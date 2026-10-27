#!/usr/bin/env python3
"""Execute the production observer module from a patched translator checkout."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

runtime = Path(sys.argv[1]) / 'crates/metal2vulkan-runtime/src/command_buffer/mod.rs'
source = runtime.read_text()
start = source.index('/// Optional bounded observer of synchronous commit phases.')
module = source[start:]
bridge_source = (Path(__file__).resolve().parent / 'NativeMetalObjects.rs').read_text()
bridge_start = bridge_source.index('#[cfg(feature = "unixnu-commit-trace")]\n#[unsafe(no_mangle)]\npub extern "C" fn nmb_command_buffer_commit_profiled')
bridge_end = bridge_source.index('#[unsafe(no_mangle)]\npub extern "C" fn nmb_command_buffer_wait(', bridge_start)
bridge = bridge_source[bridge_start:bridge_end]
test = r'''
mod holder {
MODULE
pub fn mark(kind:u32){unixnu_commit_trace::record(kind)}
}
mod metal2vulkan_runtime {pub use super::holder::unixnu_commit_trace;}
use std::sync::{Mutex,atomic::{AtomicU64,Ordering}};
static TICKS:AtomicU64=AtomicU64::new(0);
static LOG:Mutex<Vec<(u32,u64,u64)>>=Mutex::new(Vec::new());
extern "C" fn clock()->u64{TICKS.fetch_add(1,Ordering::SeqCst)+1}
extern "C" fn sink(k:u32,id:u64,t:u64){LOG.lock().unwrap().push((k,id,t));}
pub struct NmbCommandBuffer(Fake);
struct Fake;
impl Fake {fn commit(&self)->Result<(),String>{holder::mark(50);holder::mark(56);Ok(())}}
fn get<T:'static>(p:*const T)->Result<&'static T,String>{unsafe{p.as_ref()}.ok_or("null".into())}
fn e(_: &'static str)->impl Fn(String)->String { |x|x }
fn guard<T>(fallback:T,f:impl FnOnce()->Result<T,String>)->T{f().unwrap_or(fallback)}
BRIDGE
fn main(){
 holder::mark(1);assert_eq!(TICKS.load(Ordering::SeqCst),0);
 let caught=std::panic::catch_unwind(||holder::unixnu_commit_trace::capture(9,clock,sink,||{
  holder::mark(50);
  holder::unixnu_commit_trace::capture(10,clock,sink,||holder::mark(51));
  holder::mark(52);panic!("controlled test");
 }));assert!(caught.is_err());
 {let l=LOG.lock().unwrap();assert_eq!(l.as_slice(),&[(51,10,2),(50,9,1),(52,9,3)]);}
 LOG.lock().unwrap().clear();
 holder::unixnu_commit_trace::capture(11,clock,sink,||for k in 0..18{holder::mark(k)});
 {let l=LOG.lock().unwrap();assert_eq!(l.len(),17);assert_eq!(l[16].0,59);assert_eq!(l[16].1,11);}
 LOG.lock().unwrap().clear();
 holder::unixnu_commit_trace::capture(77,clock,sink,||{
  for k in 50..=55 { holder::mark(k); }
  for _ in 0..2 { for k in [60,61,62,64,65,63] { holder::mark(k); } }
  for k in [57,58,56] { holder::mark(k); }
 });
 {let l=LOG.lock().unwrap();let kinds:Vec<_>=l.iter().map(|r|r.0).collect();assert_eq!(kinds.len(),17);assert_eq!(&kinds[13..],&[57,58,56,59]);}
 LOG.lock().unwrap().clear();
 let jobs:Vec<_>=(0..4).map(|id|std::thread::spawn(move||holder::unixnu_commit_trace::capture(id,clock,sink,||holder::mark(50)))).collect();
 for j in jobs{j.join().unwrap();}
 let mut ids:Vec<_>=LOG.lock().unwrap().iter().map(|row|row.1).collect();ids.sort();assert_eq!(ids,vec![0,1,2,3]);
 LOG.lock().unwrap().clear();let mut command=NmbCommandBuffer(Fake);
 assert!(!nmb_command_buffer_commit_profiled(&mut command,12,None,Some(sink)));
 assert!(!nmb_command_buffer_commit_profiled(std::ptr::null_mut(),12,Some(clock),Some(sink)));
 assert!(nmb_command_buffer_commit_profiled(&mut command,12,Some(clock),Some(sink)));
 let log=LOG.lock().unwrap();assert_eq!(log.len(),2);assert_eq!((log[0].0,log[0].1),(50,12));assert_eq!((log[1].0,log[1].1),(56,12));
}
'''.replace('MODULE', module).replace('BRIDGE', bridge)
with tempfile.TemporaryDirectory(prefix='commit-observer-') as tmp:
    source_path = Path(tmp) / 'observer.rs'
    binary = Path(tmp) / 'observer'
    source_path.write_text(test)
    subprocess.run([os.environ.get('RUSTC', 'rustc'), '--edition=2024', '--cfg', 'feature="unixnu-commit-trace"',
                    str(source_path), '-o', str(binary)], check=True)
    subprocess.run([str(binary)], check=True)
print('PASS production observer and Rust C ABI: off, nesting, unwind, overflow, separate threads and null inputs')
