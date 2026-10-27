#!/usr/bin/env python3
"""Execute actual serveOne and snapshot comparator with destructive completion mocks."""
from pathlib import Path
import os, subprocess, tempfile, sys
r=Path(__file__).resolve().parents[1]
c=(r/'GpuScanoutClient.h').read_text(); p=(r/'GpuScanoutPresenter.h').read_text()
serve=c[c.index('    bool serveOne('):c.index('private:',c.index('    bool serveOne('))]
source=r''' 
#include <cassert>
#include <cstdint>
#include <cstddef>
#include <unistd.h>
#include <vector>
#include <thread>
#include "ScanoutVerification.h"
static uint64_t mach_absolute_time(){return 100;}
struct mach_timebase_info_data_t{unsigned numer,denom;};
static void mach_timebase_info(mach_timebase_info_data_t *p){p->numer=p->denom=1;}
struct NGPWait{uint64_t timeout;};
struct NGPSwap{uint64_t sequence,address,bytes,stride;uint32_t width,height;};
struct NGPComplete{uint64_t sequence;uint32_t status,reserved;};
enum{NGP_METHOD_WAIT_SWAP,NGP_METHOD_COMPLETE_SWAP};
static bool complete=false, captureOK=true, completeOK=true;
static unsigned captured=0,compared=0;
static uint32_t live[8]={1,2,3,4,1,2,3,4};
namespace gpupresent{
enum Result{kOk,kGpuFailed};
struct Frame{const void *base;size_t bytes,stride;uint32_t width,height;};
struct CopyTrace{};
struct Presenter {
std::vector<uint32_t> snapshot; bool pending=false;
bool verificationAvailable(){return !pending;}
Result present(const Frame&,CopyTrace*){complete=false;return kOk;}
bool captureVerification(const Frame&){assert(!complete);++captured;snapshot.assign(live,live+8);return captureOK;}
void compareVerificationAsync(){assert(complete);pending=true;}
bool consumeVerification(uint64_t &value){if(!pending)return false; ++compared;value=VerificationResult::compare(snapshot.data(),2,2);pending=false;return true;}
};}
struct Stats{uint64_t frames=0,failures=0,timeouts=0,verified=0,mismatchRows=0,totalMs=0;int error=0,lastResult=0;uint32_t lastMs=0;};
struct Transport{void *context=nullptr;int(*call)(void*,uint32_t,const void*,size_t,void*,size_t);};
static int call(void*,uint32_t method,const void*,size_t,void *out,size_t){
if(method==NGP_METHOD_WAIT_SWAP){*(NGPSwap*)out={1,(uintptr_t)live,4096,8,2,2};return 0;}
complete=true;for(auto &x:live)x=999;return completeOK?0:1;
}
struct Client{
Stats *stats_;Transport transport_;gpupresent::Presenter *presenter_;gpupresent::CopyTrace *copyTrace_=nullptr;
void (*latency_)(uint32_t,uint64_t,uint64_t)=nullptr;
bool verificationReady_=true;unsigned verificationAttempts_=0;void note(unsigned){}
SERVE
};
int main(){
gpupresent::Presenter p;Stats s;Client c{&s,{nullptr,call},&p};
assert(c.serveOne(1));assert(s.verified==0&&s.frames==1);
// Completion invalidated the original live mapping; comparison still used the snapshot.
live[0]=1;live[4]=2;assert(c.serveOne(1));assert(s.mismatchRows==0&&s.verified==1);
captureOK=false;assert(c.serveOne(1));assert(s.verified==2&&s.mismatchRows==1);captureOK=true;
completeOK=false;assert(!c.serveOne(1));assert(s.verified==2&&s.failures==1);completeOK=true;
unsigned before=captured;assert(c.serveOne(1));assert(captured==before); // fixed attempt cap
Stats off;Client d{&off,{nullptr,call},&p};d.verificationReady_=false;
assert(d.serveOne(1));assert(captured==before&&off.verified==0&&off.frames==1);
gpupresent::VerificationResult result;
for(unsigned iteration=0;iteration<100;iteration++) {
 result.busy.store(true);uint64_t value=0;
 std::thread worker([&]{result.publish(123);});
 while(result.busy.load(std::memory_order_acquire))std::this_thread::yield();
 assert(result.consume(value)&&value==123);assert(!result.consume(value));worker.join();
}
uint32_t a[]={1,2,3,4,0,0,0,0};assert(gpupresent::VerificationResult::compare(a,2,2)==2);
}
'''.replace('SERVE',serve)
with tempfile.TemporaryDirectory() as directory:
 t=Path(directory);(t/'test.cpp').write_text(source)
 includes=[]
 if sys.platform.startswith('linux') and 'clang' in os.environ.get('CXX','c++'):
  search=subprocess.run(['c++','-E','-v','-x','c++','-'],input='',text=True,capture_output=True,check=True).stderr
  includes += ['-L',str(Path(subprocess.check_output(['c++','-print-file-name=libstdc++.so'],text=True).strip()).parent)]
  for line in search.split('#include <...> search starts here:')[1].split('End of search list.')[0].splitlines():
   if line.strip(): includes += ['-isystem',line.strip()]
 subprocess.run([os.environ.get('CXX','c++'),*includes,'-I',str(r),'-std=c++17','-pthread','-Wall','-Wextra','-Werror','-fsanitize=address,undefined','-fno-omit-frame-pointer',str(t/'test.cpp'),'-o',str(t/'test')],check=True)
 subprocess.run([str(t/'test')],check=True)
# Capture must finish before lease release; CPU comparison must follow successful release.
assert serve.index('captureVerification')<serve.index('NGP_METHOD_COMPLETE_SWAP')<serve.index('compareVerificationAsync')
assert 'volatile uint32_t' not in c
assert c.index('prepareVerification()')<c.index('bool serveOne(')
print('PASS: actual serveOne/comparator/publication; destructive completion, mismatch rows, failure paths, four-attempt cap, default off; ASan/UBSan')
