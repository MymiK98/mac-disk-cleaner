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
