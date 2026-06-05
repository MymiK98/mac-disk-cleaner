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

    def test_symlinks_are_excluded(self):
        root = tempfile.mkdtemp()
        real = os.path.join(root, "real.bin")
        with open(real, "wb") as f:
            f.write(b"x" * 100)
        os.symlink(real, os.path.join(root, "link.bin"))
        self.assertEqual(disk_cleaner.dir_size(root), 100)


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
        self.assertIn("/tmp/foo bar", cmd[2])
        self.assertIn("Finder", cmd[2])

    def test_backslash_in_path_escaped(self):
        cmd = disk_cleaner.move_command("/tmp/foo\\")
        script = cmd[2]
        # backslash doubled, so quoted string is not terminated early
        self.assertIn('"/tmp/foo\\\\"', script)

    def test_protected_path_refused(self):
        with self.assertRaises(disk_cleaner.ProtectedPathError):
            disk_cleaner.move_to_trash("/System")


class ScanPathsTest(unittest.TestCase):
    def test_builds_items_for_existing_paths_only(self):
        root = tempfile.mkdtemp()
        big = os.path.join(root, "cache")
        os.makedirs(big)
        with open(os.path.join(big, "f.bin"), "wb") as f:
            f.write(b"a" * 2048)
        missing = os.path.join(root, "nope")

        items = disk_cleaner.scan_paths(
            [big, missing], category="dev_cache", default_checked=True)

        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["path"], big)
        self.assertEqual(item["size"], 2048)
        self.assertEqual(item["category"], "dev_cache")
        self.assertTrue(item["default_checked"])
        self.assertEqual(item["label"], "cache")


class LargeFilesTest(unittest.TestCase):
    def test_finds_only_files_over_threshold(self):
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "big.bin"), "wb") as f:
            f.write(b"a" * 3000)
        with open(os.path.join(root, "small.bin"), "wb") as f:
            f.write(b"a" * 10)

        items = disk_cleaner.scan_large_files(root, threshold=1000)

        paths = [i["path"] for i in items]
        self.assertIn(os.path.join(root, "big.bin"), paths)
        self.assertNotIn(os.path.join(root, "small.bin"), paths)
        self.assertFalse(items[0]["default_checked"])
        self.assertEqual(items[0]["category"], "large_files")

    def test_recurses_into_subdirectories(self):
        root = tempfile.mkdtemp()
        sub = os.path.join(root, "deep", "nested")
        os.makedirs(sub)
        buried = os.path.join(sub, "buried.bin")
        with open(buried, "wb") as f:
            f.write(b"a" * 3000)
        items = disk_cleaner.scan_large_files(root, threshold=1000)
        self.assertIn(buried, [i["path"] for i in items])


class DuplicatesTest(unittest.TestCase):
    def test_flags_copies_not_original(self):
        root = tempfile.mkdtemp()
        content = b"identical-data" * 100
        for name in ["orig.bin", "copy1.bin", "copy2.bin"]:
            with open(os.path.join(root, name), "wb") as f:
                f.write(content)
        with open(os.path.join(root, "unique.bin"), "wb") as f:
            f.write(b"different")

        items = disk_cleaner.scan_duplicates([root])

        paths = sorted(os.path.basename(i["path"]) for i in items)
        self.assertEqual(len(items), 2)
        self.assertNotIn("unique.bin", paths)
        self.assertTrue(all(i["category"] == "duplicates" for i in items))
        self.assertTrue(all(not i["default_checked"] for i in items))

    def test_same_size_different_content_not_flagged(self):
        root = tempfile.mkdtemp()
        with open(os.path.join(root, "a.bin"), "wb") as f:
            f.write(b"A" * 500)
        with open(os.path.join(root, "b.bin"), "wb") as f:
            f.write(b"B" * 500)
        items = disk_cleaner.scan_duplicates([root])
        self.assertEqual(items, [])


class InventoryTest(unittest.TestCase):
    def test_returns_all_categories_and_total(self):
        inv = disk_cleaner.build_inventory()
        self.assertIn("categories", inv)
        keys = {c["key"] for c in inv["categories"]}
        self.assertEqual(
            keys,
            {"system_cache", "dev_cache", "large_files", "duplicates"},
        )
        for c in inv["categories"]:
            self.assertIn("items", c)
            self.assertIn("size", c)
        self.assertIn("disk", inv)


class DeletePathsTest(unittest.TestCase):
    def test_sums_freed_and_records_failures(self):
        root = tempfile.mkdtemp()
        good = os.path.join(root, "good.bin")
        with open(good, "wb") as f:
            f.write(b"x" * 1000)

        moved = []

        def fake_mover(path):
            moved.append(path)

        result = disk_cleaner.delete_paths([good], mover=fake_mover)

        self.assertEqual(result["freed"], 1000)
        self.assertEqual(result["failed"], [])
        self.assertEqual(moved, [good])

    def test_failure_recorded_not_raised(self):
        def boom(path):
            raise RuntimeError("denied")

        result = disk_cleaner.delete_paths(["/tmp/whatever"], mover=boom)
        self.assertEqual(result["freed"], 0)
        self.assertEqual(len(result["failed"]), 1)
        self.assertEqual(result["failed"][0]["path"], "/tmp/whatever")
