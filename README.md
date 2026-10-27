# NativeMetal

Metal API frontend. Publishes a Metal device through `MTLAddDevice`, translates AIR with metal2vulkan and submits through Turnip and NativeAdreno.

Current state: the GPU-composited desktop runs on sheng hardware. First-use shader compilation is slow and there is no complete precompiled UI pack.

Built into the iPadOS plugin and the offline host instruments.

## First scanout copy timing (optional)

`PVR_METAL_PIPELINE_DIAG=1` enables a fixed first-valid-copy record in the Vulkan
plugin. The default is off. Timing records add no buffer reads or copy-path logging.
The existing status thread observes once per second until the first copy returns,
then resumes 20-second sleeps. ST/PUB retain their 20-second cadence. Copy output
is bounded to the first eight observations plus phase/return and encoder-mask
transitions, with no copy-path output. At most 39 reports of 10 lines
are emitted (eight periodic reports plus at most five phase, one return and
25 mask transitions); events may be coalesced in a single report. The first
two timing lines are:

- `CP<phase> e<elapsed-ms> r<returned>`: phase 1 started, 2 source import returned,
  3 encoding returned, 4 commit returned, 5 wait returned.
- `CT i<ms> n<ms> c<ms> w<ms>`: completed import, encode, commit and wait
  intervals. An unfinished interval is zero; use the phase to disambiguate it.

Elapsed time continues while the copy is blocked and freezes on return (at the
wait boundary for phase 5). `r1` means present() returned, including local failures;
it does not mean success. Observation timing depends on scheduler availability.
Millisecond output saturates at UINT32_MAX. Records are never replaced by later
frames; bad scanout/frame validation returns before arming the record. A local failure or exception can leave an intermediate phase; consult
PF/PX and exception diagnostics before interpreting it as a pending operation.
Phase 5 means wait returned, not successful command status, COMPLETE_SWAP return,
IOMFB retirement, or visually verified display. The record excludes WAIT_SWAP
and its kernel mapping work. None of these times represents native vblank.

Host check: `python3 tests/check_scanout_copy_trace.py` (C++17, ASan/UBSan).

The same first-copy report includes `CG w<width> h<height> a<alignment> r<route>`
and `CS s<source-pitch> d<target-pitch>` (bytes). Routes: 0 contiguous buffer copy,
1 separate Metal buffer copies per row, 2 one buffer-texture-to-buffer copy,
3 not selected yet. These describe the actual Presenter branch, not measured
backend job counts. For route 1 the runtime records separate one-region Vulkan
copies; route 2 builds one multi-region copy, merging rows contiguous in both
source and destination. Region/job counts must not be inferred from allocation
size alone. No frame contents are read for these records.

With `PVR_METAL_PIPELINE_DIAG=1`, the client also enables optional pixel
verification. Storage for two packed top-400-row snapshots is allocated before
WAIT_SWAP. After a successful display copy, the GPU reads both the offered
source and the actual scanout into that storage while the swap lease still
protects the source mapping. Unsupported geometry, allocation or capture failure
does not change the display-copy result and does not increment `PF v`.
There are at most four capture attempts, including failed attempts.

Only after COMPLETE_SWAP succeeds is CPU comparison dispatched to a background
worker. The block retains the snapshot buffer and result record, never a live
source/scanout mapping or Client/Presenter pointer. While comparison or result
consumption is pending the slot cannot be overwritten; display serving continues
without waiting for the worker. The serving thread consumes the atomic result
on a subsequent serveOne call and updates the existing PF verified/mismatch-row
counters. A client that exits before consumption may leave the result unreported.
A completed capture after a failed COMPLETE_SWAP is never reported as verified.

The comparison covers all 32-bit pixels in the top 400 rows of each captured
frame, counting each differing row once. It excludes padding and the lower
diagnostic drawing area. This is GPU-readback snapshot evidence, not the old
CPU-direct framebuffer-load evidence, physical display scanout proof, or CPU
rendering. Snapshot capture still adds GPU work before COMPLETE_SWAP; its wait
has the ordinary Metal command-completion contract, with no fake success or
timeout shortcut. CP/CT and lastMs exclude both capture and CPU comparison;
PF/frame retirement and the actual image must be checked separately. The default
profile allocates no verification storage and dispatches no comparison work.

Host checks (also run with the macOS SDK compiler before deployment):
`python3 tests/check_scanout_copy_trace.py` and
`CXX=clang++ python3 tests/check_scanout_verification.py`.
The latter executes production serveOne, comparison and publication code with
completion that destroys the original mapping contents, capture/completion
failures, the attempt cap and default-off operation. Metal API syntax is mocked;
real GPU snapshot legality/coherency and deadline margin remain hardware checks.

## First scanout blit encoder-open timing (optional)

When `PVR_METAL_PIPELINE_DIAG=1`, the first scanout's `blitCommandEncoder`
call temporarily installs a callback on that calling thread. The patched pinned
metal2vulkan runtime marks the boundaries below. The callback is cleared on
scope exit, including local errors. Other threads and
subsequent frames do not inherit it. The disabled path reads no diagnostic
clock; the runtime still checks a thread-local callback slot at boundaries.

`mach_absolute_time` is converted with `mach_timebase_info`; it is a monotonic
host clock. `present` is called serially by one scanout thread. The frontend
calls the bridge synchronously on that same thread. A scope mask with only
24/25 and no runtime events indicates the hook did not observe that path; it
does not prove the Vulkan operations did not happen.

The existing status observer emits these bounded lines alongside CP/CT:

- `EL m<mask> l<us> p<us>`: state-lock wait and spare-pool-lock wait.
- `EP r<us> c<us>`: pool reset and pool create duration.
- `EA a<us> b<us>`: raw command allocation and Vulkan begin duration.
- `ES s<us> f<us>`: whole scoped call, and state-lock entry through encoder-state construction start.

The hexadecimal mask has bit `event - 1` set for each observed event:

| Event | Meaning |
| --- | --- |
| 1, 2 | state-lock request, acquired |
| 3, 4 | spare-pool lock request, acquired |
| 5, 6 | no spare, spare selected |
| 7, 8 | reset entered, returned |
| 9, 10 | create entered, returned |
| 11 | existing raw command buffer reused |
| 12, 13 | raw allocation entered, returned |
| 14, 15 | Vulkan begin entered, returned |
| 16 | spare lookup bypassed due to an exit section |
| 17 | encoder state refused |
| 18, 19, 20, 21 | reset, create, raw allocation, begin failed |
| 22 | begin succeeded; about to construct encoder state |
| 23 | command buffer already had a pool |
| 24, 25 | Objective-C scoped call entered, returned |

Durations are microseconds (truncated by the clock); `4294967295` means not
started or an invalid/reversed clock, and `4294967294` is the saturation value.
For a started operation whose end is not yet seen, the duration is the elapsed
time as of the snapshot, frozen at scope return. The mask distinguishes a
completed duration from a pending or bypassed one. A mask change triggers a
bounded additional report; event bits are first-wins. The lock intervals include
the instrumentation and possible preemption while the locks are held. The call's final result
still comes from the existing failure/status reporting. Times are host wall
intervals and include preemption and instrumentation, not GPU timestamps. No
pixel, resource ownership, pool lifecycle, or failure behavior is changed.
The runtime patch is tied to translator revision
`720ef1ce9ae2a2068999a8675717418b9416283e`; the bridge must be rebuilt with it.

Host checks: `python3 tests/check_scanout_copy_trace.py` and
`python3 tests/check_encoder_open_trace.py --translator /path/to/patched/metal2vulkan`, and
`python3 tests/check_encoder_presenter_mock.py`.
These do not establish actual Metal/GPU correctness or timing.
