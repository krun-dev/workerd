import importlib.util
from pathlib import Path
import subprocess
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('versioning', Path(__file__).parents[1] / 'scripts/versioning.py')
versioning = importlib.util.module_from_spec(spec)
spec.loader.exec_module(versioning)


class VersionTests(unittest.TestCase):
    def test_first_release_and_migration_start_at_zero(self):
        for current in ['', '1.20260916.1-cpu.1']:
            self.assertEqual(versioning.next_version('1.20260916.1', current, []), '1.20260916.1-krun.0')

    def test_same_upstream_increments(self):
        self.assertEqual(versioning.next_version('1.20260916.1', '1.20260916.1-krun.1', []),
                         '1.20260916.1-krun.2')

    def test_new_date_or_upstream_patch_resets(self):
        for upstream in ['1.20260917.1', '1.20260916.2']:
            self.assertEqual(versioning.next_version(upstream, '1.20260916.1-krun.9',
                                                    ['v1.20260916.1-krun.10']), upstream + '-krun.0')

    def test_tag_revisions_sort_numerically(self):
        self.assertEqual(versioning.next_version('1.20260916.1', '1.20260916.1-krun.0',
                         ['v1.20260916.1-krun.9', 'v1.20260916.1-krun.10', 'v1.20260917.1-krun.20']),
                         '1.20260916.1-krun.11')

    def test_return_to_old_upstream_does_not_reuse_tag(self):
        self.assertEqual(versioning.next_version('1.20260916.1', '1.20260917.1-krun.0',
                         ['v1.20260916.1-krun.2']), '1.20260916.1-krun.3')

    def test_reject_bad_versions_and_mismatch(self):
        for value in ['1.20260916.1-cpu.1', '1.20260916.1+krun.0', '1.20260916.1-krun.01',
                      'v1.20260916.1-krun.0', '1.20260916.1-krun.-1']:
            with self.assertRaises(ValueError):
                versioning.parse(value)
        with self.assertRaises(ValueError):
            versioning.validate('1.20260916.1-krun.0', '1.20260917.1')
        versioning.validate('1.20260916.1-krun.0', '1.20260916.1')

    def test_upstream_must_be_exact_tag(self):
        with tempfile.TemporaryDirectory() as directory:
            def git(*args):
                return subprocess.run(['git', *args], cwd=directory, check=True, capture_output=True)
            git('init')
            git('-c', 'user.name=Test', '-c', 'user.email=test@example.test', 'commit', '--allow-empty', '-m', 'base')
            with self.assertRaises(ValueError):
                versioning.upstream_version(directory)
            git('tag', 'v1.20260916.1')
            self.assertEqual(versioning.upstream_version(directory), '1.20260916.1')
            git('tag', 'v1.20260916.2')
            with self.assertRaises(ValueError):
                versioning.upstream_version(directory)


if __name__ == '__main__':
    unittest.main()
