#!/usr/bin/env python3
"""Compile the production family-policy methods against a bounded backend fixture."""
from pathlib import Path
import subprocess, tempfile
root=Path(__file__).resolve().parents[1]
s=(root/'bridge/NativeMetalObjects.rs').read_text()
start=s.index('const TARGET_FAMILIES:')
end=s.index('/// The rows of Apple\'s tables',start)
policy=s[start:end]
assert '"supportsFamily:" => supports_family(device, arg as i64) as u64' in s
assert 'for family in TARGET_FAMILIES {\n            if !target_family_override(device, family) { continue; }' in s
assert 'AtomicBool::new(false)' in s
front=(root/'NativeBridgeMetalFrontend.mm').read_text()
assert front.index('nmb_device_require_fetch_for_apple3(gDevice,') < front.index('nmb_device_family_waivers(gDevice,')
fixture=r'''
use std::sync::atomic::AtomicBool;
#[derive(Clone,Copy,PartialEq,Eq)] enum GpuFamily { Apple1, Apple2, Apple3, Common1, Common2 }
impl GpuFamily { fn raw(self)->i64 {match self {Self::Apple1=>1001,Self::Apple2=>1002,Self::Apple3=>1003,Self::Common1=>3001,Self::Common2=>3002}} }
enum PrivateFlag { IsFramebufferReadSupported }
struct Flag(Option<bool>); impl Flag {fn value(self)->Option<bool>{self.0}}
struct Private(Option<bool>); impl Private {fn flag(&self,_:PrivateFlag)->Flag {Flag(self.0)}}
struct Backend(bool); impl Backend {fn supports_family(&self,f:i64)->bool {self.0 && f==1003}}
struct NmbDevice(Backend,Private,(),AtomicBool);
impl NmbDevice {fn private(&self)->&Private{&self.1}}
'''
checks=r'''
fn main(){
 for enabled in [false,true] {for native in [false,true] {for fetch in [None,Some(false),Some(true)] {
  let d=NmbDevice(Backend(native),Private(fetch),(),AtomicBool::new(enabled));
  assert_eq!(supports_family(&d,1003),native || !enabled || fetch==Some(true));
  for family in TARGET_FAMILIES {
   let waiver=target_family_override(&d,family);
   assert_eq!(waiver,family!=GpuFamily::Apple3 || !enabled || fetch==Some(true));
   assert_eq!(supports_family(&d,family.raw()),d.0.supports_family(family.raw()) || waiver);
  }
  assert!(!supports_family(&d,1005)); assert!(!supports_family(&d,-1));
 }}}
}
'''
with tempfile.TemporaryDirectory(prefix='nmb-family-policy-') as t:
 p=Path(t); (p/'policy.rs').write_text(fixture+policy+checks)
 subprocess.run(['rustc','--edition=2024','-Dwarnings',str(p/'policy.rs'),'-o',str(p/'test')],check=True)
 subprocess.run([str(p/'test')],check=True)
print('PASS production policy methods: default/off/on, native support preserved, fetch true/false/unknown, Apple2 unchanged, unknown families, selector/waiver consistency and pre-publication ordering')
