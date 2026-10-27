# NativeMetal

Metal API frontend. Publishes a Metal device through `MTLAddDevice`, translates AIR with metal2vulkan and submits through Turnip and NativeAdreno.

Current state: the GPU-composited desktop runs on sheng hardware. First-use shader compilation is slow and there is no complete precompiled UI pack.

Built into the iPadOS plugin and the offline host instruments.

## First scanout copy timing (optional)

`PVR_METAL_PIPELINE_DIAG=1` enables a fixed first-copy record in the Vulkan
plugin. The default is off. Timing records add no BO reads or copy-path logging. The existing status thread observes once per second until the first copy returns,
then resumes 20-second sleeps. ST/PUB retain their 20-second cadence. Copy output
is bounded to the first eight observations plus phase/return transitions (at most
13 pairs), with no copy-path output. The two lines are:

- `CP<phase> e<elapsed-ms> r<returned>`: phase 1 started, 2 source import returned,
  3 encoding returned, 4 commit returned, 5 wait returned.
- `CT i<ms> n<ms> c<ms> w<ms>`: completed import, encode, commit and wait
  intervals. An unfinished interval is zero; use the phase to disambiguate it.

Elapsed time continues while the copy is blocked and freezes on return (at the
wait boundary for phase 5). `r1` means present() returned, including local failures;
it does not mean success. Observation timing depends on scheduler availability.
Millisecond output saturates at UINT32_MAX. Records are never replaced by later
frames. A local failure or exception can leave an intermediate phase; consult
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
