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
print('PASS: production pool policy defaults, accepted sizes, invalid inputs and device limits')
