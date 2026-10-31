#!/usr/bin/env python3
"""Compile and execute the allocator policy from the recorded production patch."""
from pathlib import Path
import subprocess
import tempfile
root = Path(__file__).resolve().parents[1]
patch = (root / 'bridge/patches/0008-optional-memory-pool-size.patch').read_text()
added = '\n'.join(line[1:] for line in patch.splitlines() if line.startswith('+') and not line.startswith('+++'))
start = added.index('fn configured_block_bytes(')
end = added.index('\n}', start) + 2
function = added[start:end]
source = 'const BLOCK_BYTES: u64 = 64 << 20;\n' + function + '''
fn main() {
    for value in [None, Some(""), Some("0"), Some("3"), Some("128"), Some("-1"), Some("16x"), Some("18446744073709551616")] {
        assert_eq!(configured_block_bytes(value), 64 << 20);
    }
    for mib in [4u64, 8, 16, 32, 64] {
        assert_eq!(configured_block_bytes(Some(&mib.to_string())), mib << 20);
        for limit in [1, 4096, 8 << 20, 128 << 20] {
            assert_eq!(configured_block_bytes(Some(&mib.to_string())).min(limit), (mib << 20).min(limit));
        }
    }
}
'''
with tempfile.TemporaryDirectory() as directory:
    path = Path(directory)
    (path / 'policy.rs').write_text(source)
    subprocess.run(['rustc', '--edition=2021', str(path / 'policy.rs'), '-o', str(path / 'policy')], check=True)
    subprocess.run([str(path / 'policy')], check=True)
    # NMB_POOL_BLOCK_MIB=auto, resolved by the frontend (NativeMetalPoolSize.h) into a value the patch above accepts.
    (path / 'auto.c').write_text('#include "NativeMetalPoolSize.h"\n#include <assert.h>\nint main(void){\n'
        ' const uint64_t M=1ull<<20;\n'
        ' struct {uint64_t memsize; unsigned mib;} rows[]={{0,64},{1,4},{256*M,4},{512*M,4},{1023*M,4},{1024*M,8},{1344*M,8},'
        '{2047*M,8},{2048*M,16},{3072*M,16},{4096*M,32},{6144*M,32},{8192*M,64},{16384*M,64},{~0ull,64}};\n'
        ' for(unsigned i=0;i<sizeof rows/sizeof rows[0];i++)assert(nmb_pool_block_mib_for_memsize(rows[i].memsize)==rows[i].mib);\n'
        ' unsetenv("NMB_POOL_BLOCK_MIB");assert(!nmb_pool_block_resolve_auto(2048*M)&&!getenv("NMB_POOL_BLOCK_MIB"));\n'
        ' setenv("NMB_POOL_BLOCK_MIB","16",1);assert(!nmb_pool_block_resolve_auto(1024*M)&&!strcmp(getenv("NMB_POOL_BLOCK_MIB"),"16"));\n'
        ' setenv("NMB_POOL_BLOCK_MIB","AUTO",1);assert(!nmb_pool_block_resolve_auto(1024*M)&&!strcmp(getenv("NMB_POOL_BLOCK_MIB"),"AUTO"));\n'
        ' setenv("NMB_POOL_BLOCK_MIB","auto",1);assert(nmb_pool_block_resolve_auto(1344*M)==8&&!strcmp(getenv("NMB_POOL_BLOCK_MIB"),"8"));\n'
        ' setenv("NMB_POOL_BLOCK_MIB","auto",1);assert(nmb_pool_block_resolve_auto(0)==64&&!strcmp(getenv("NMB_POOL_BLOCK_MIB"),"64"));\n'
        ' return 0;}\n')
    subprocess.run(['cc', '-std=c11', '-D_POSIX_C_SOURCE=200809L', '-Wall', '-Wextra', '-Werror', '-I', str(root),
                    str(path / 'auto.c'), '-o', str(path / 'auto')], check=True)
    subprocess.run([str(path / 'auto')], check=True)
print('PASS: production pool policy defaults, accepted sizes, invalid inputs and device limits; auto sizes from memory')
