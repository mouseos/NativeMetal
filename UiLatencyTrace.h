// SPDX-License-Identifier: MIT
#pragma once
#include <stdint.h>
// Fixed diagnostic queue. Try-lock only: a contended producer/consumer never waits.
// Callers supply the timestamp; output is drained outside all runtime/VM locks.
namespace uilatency {
struct Event { uint64_t tick,id; uint32_t kind,work; };
class Trace {
    unsigned lock_ = 0, head_ = 0, count_ = 0;
    uint64_t lost_ = 0;
    Event rows_[256] = {};
public:
    bool push(Event event) {
        if (__atomic_exchange_n(&lock_,1u,__ATOMIC_ACQUIRE)) {__atomic_fetch_add(&lost_,1ULL,__ATOMIC_RELAXED);return false;}
        bool accepted = count_ < 256;
        if (accepted) {rows_[(head_+count_)%256]=event;++count_;}
        else __atomic_fetch_add(&lost_,1ULL,__ATOMIC_RELAXED);
        __atomic_store_n(&lock_,0u,__ATOMIC_RELEASE);return accepted;
    }
    bool pop(Event &event) {
        if (__atomic_exchange_n(&lock_,1u,__ATOMIC_ACQUIRE)) return false;
        bool available = count_ != 0;
        if (available) {event=rows_[head_];head_=(head_+1)%256;--count_;}
        __atomic_store_n(&lock_,0u,__ATOMIC_RELEASE);return available;
    }
    uint64_t lost() const {return __atomic_load_n(&lost_,__ATOMIC_RELAXED);}
};
}
