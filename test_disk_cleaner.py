import os
import tempfile
import unittest

import disk_cleaner


class DirSizeTest(unittest.TestCase):
    def test_sums_file_sizes_recursively(self):
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "a.bin"), "wb") as f:
            f.write(b"x" * 1000)
        sub = os.path.join(root, "sub")
        os.makedirs(sub)
        with open(os.path.join(sub, "b.bin"), "wb") as f:
            f.write(b"y" * 500)
        self.assertEqual(disk_cleaner.dir_size(root), 1500)

    def test_single_file_returns_its_size(self):
        fd, path = tempfile.mkstemp()
        os.write(fd, b"z" * 42)
        os.close(fd)
        self.assertEqual(disk_cleaner.dir_size(path), 42)

    def test_missing_path_returns_zero(self):
        self.assertEqual(disk_cleaner.dir_size("/no/such/path/xyz"), 0)


class ProtectedPathTest(unittest.TestCase):
    def test_system_roots_are_protected(self):
        for p in ["/System", "/Library", "/usr", "/bin", "/Applications"]:
            self.assertTrue(disk_cleaner.is_protected(p), p)

    def test_home_root_and_library_protected(self):
        home = os.path.expanduser("~")
        self.assertTrue(disk_cleaner.is_protected(home))
        self.assertTrue(disk_cleaner.is_protected(os.path.join(home, "Library")))

    def test_parent_of_protected_is_protected(self):
        self.assertTrue(disk_cleaner.is_protected("/"))

    def test_cache_subfolder_not_protected(self):
        home = os.path.expanduser("~")
        target = os.path.join(home, "Library", "Caches", "com.apple.Safari")
        self.assertFalse(disk_cleaner.is_protected(target))


class MoveToTrashTest(unittest.TestCase):
    def test_command_targets_posix_path(self):
        cmd = disk_cleaner.move_command("/tmp/foo bar")
        self.assertEqual(cmd[0], "osascript")
        joined = " ".join(cmd)
        self.assertIn("/tmp/foo bar", joined)
        self.assertIn("Finder", joined)

    def test_protected_path_refused(self):
        with self.assertRaises(disk_cleaner.ProtectedPathError):
            disk_cleaner.move_to_trash("/System")
