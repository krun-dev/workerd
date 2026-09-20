"""Verify downloaded gzip content and the release checksum manifest without compiling."""
import gzip
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1] / 'scripts'))
import distro


class PackageTests(unittest.TestCase):
    def test_compressed_binary_and_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            build = root / '.build'
            upstream = root / 'upstream'
            for path in [build, upstream, root / 'patches']:
                path.mkdir()
            for name in ['README.md', 'CHANGES.md', 'VERSION']:
                (root / name).write_text('fixture')
            (upstream / 'LICENSE').write_text('fixture license')
            (root / 'patches/series').write_text('fixture')
            binary = b'fixture executable bytes\x00\x01'
            (build / 'workerd-release').write_bytes(binary)
            inputs = {'version': '1.20260916.1-krun.0'}
            digest = hashlib.sha256(binary).hexdigest()
            (build / 'build.json').write_text(json.dumps(dict(inputs, binary_sha256=digest,
                    distro_dirty=False, distro_commit='abc')))
            (build / 'test.json').write_text(json.dumps({'inputs': inputs, 'binary_sha256': digest}))
            with patch.multiple(distro, ROOT=root, BUILD=build, UPSTREAM=upstream), \
                    patch.object(distro, 'inputs', return_value=inputs), \
                    patch.object(distro, 'output', side_effect=lambda args: 'abc' if args[1] == 'rev-parse' else ''):
                distro.package()
                self.assertEqual(gzip.decompress((root / 'dist/workerd-linux-64.gz').read_bytes()), binary)
                manifest = next((root / 'dist').glob('*.sha256'))
                entries = [line.split() for line in manifest.read_text().splitlines()]
                self.assertEqual(len(entries), 4)
                self.assertIn('workerd-linux-64.gz', [name for _, name in entries])
                for checksum, name in entries:
                    self.assertEqual(hashlib.sha256((root / 'dist' / name).read_bytes()).hexdigest(), checksum)
                (build / 'workerd-release').write_bytes(b'untested replacement')
                with self.assertRaisesRegex(RuntimeError, 'exact binary'):
                    distro.package()


if __name__ == '__main__':
    unittest.main()
