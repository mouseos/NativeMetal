# NativeMetal

Metal API frontend. Publishes a Metal device through `MTLAddDevice`, translates AIR with metal2vulkan and submits through Turnip and NativeAdreno.

Current state: the GPU-composited desktop runs on sheng hardware. First-use shader compilation is slow and there is no complete precompiled UI pack.

Built into the iPadOS plugin and the offline host instruments.

## First scanout copy timing (optional)

`PVR_METAL_PIPELINE_DIAG=1` enables a fixed first-copy record in the Vulkan
plugin. The default is off; no new thread, BO reads or copy-path logging are
introduced. The existing 20-second status thread prints two bounded lines:

- `CP<phase> e<elapsed-ms>`: phase 1 started, 2 source import returned,
  3 encoding returned, 4 commit returned, 5 wait returned.
- `CT i<ms> n<ms> c<ms> w<ms>`: completed import, encode, commit and wait
  intervals. An unfinished interval is zero; use the phase to disambiguate it.

Elapsed time continues while the copy is blocked and freezes at phase 5.
Millisecond output saturates at UINT32_MAX. Records are never replaced by later
frames. A local failure or exception can leave an intermediate phase; consult
PF/PX and exception diagnostics before interpreting it as a pending operation.
Phase 5 means wait returned, not successful command status, COMPLETE_SWAP return,
IOMFB retirement, or visually verified display. The record excludes WAIT_SWAP
and its kernel mapping work. None of these times represents native vblank.

Host check: `python3 tests/check_scanout_copy_trace.py` (C++17, ASan/UBSan).
