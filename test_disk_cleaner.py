import errno
import http.client
import json
import os
import tempfile
import threading
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
    def test_moves_file_into_trash_dir(self):
        root = tempfile.mkdtemp()
        trash = tempfile.mkdtemp()
        src = os.path.join(root, "junk.bin")
        with open(src, "wb") as f:
            f.write(b"x" * 100)

        dest = disk_cleaner.move_to_trash(src, trash_dir=trash)

        self.assertFalse(os.path.exists(src))
        self.assertTrue(os.path.exists(dest))
        self.assertEqual(os.path.dirname(dest), trash)
        self.assertEqual(os.path.basename(dest), "junk.bin")

    def test_name_collision_gets_suffix(self):
        root = tempfile.mkdtemp()
        trash = tempfile.mkdtemp()
        # pre-existing file in trash with the same name
        with open(os.path.join(trash, "dup.bin"), "wb") as f:
            f.write(b"old")
        src = os.path.join(root, "dup.bin")
        with open(src, "wb") as f:
            f.write(b"new")

        dest = disk_cleaner.move_to_trash(src, trash_dir=trash)

        self.assertTrue(os.path.exists(dest))
        self.assertNotEqual(os.path.basename(dest), "dup.bin")
        # original pre-existing file untouched
        with open(os.path.join(trash, "dup.bin"), "rb") as f:
            self.assertEqual(f.read(), b"old")

    def test_protected_path_refused(self):
        with self.assertRaises(disk_cleaner.ProtectedPathError):
            disk_cleaner.move_to_trash("/System")

    def test_permission_error_not_copied(self):
        # EPERM from rename must NOT fall back to shutil.move (which would
        # duplicate gigabytes and still fail). It should propagate.
        root = tempfile.mkdtemp()
        trash = tempfile.mkdtemp()
        src = os.path.join(root, "locked")
        os.makedirs(src)
        with open(os.path.join(src, "f.bin"), "wb") as f:
            f.write(b"x" * 10)

        orig_rename, orig_move = os.rename, disk_cleaner.shutil.move
        moved = []
        os.rename = lambda *a, **k: (_ for _ in ()).throw(
            OSError(errno.EPERM, "Operation not permitted"))
        disk_cleaner.shutil.move = lambda *a, **k: moved.append(a)
        try:
            with self.assertRaises(OSError):
                disk_cleaner.move_to_trash(src, trash_dir=trash)
        finally:
            os.rename, disk_cleaner.shutil.move = orig_rename, orig_move
        self.assertEqual(moved, [])  # never copied

    def test_cross_volume_falls_back_to_move(self):
        # EXDEV (different volume) is the one case where copy+remove is valid.
        root = tempfile.mkdtemp()
        trash = tempfile.mkdtemp()
        src = os.path.join(root, "x.bin")
        with open(src, "wb") as f:
            f.write(b"x" * 10)

        orig_rename, orig_move = os.rename, disk_cleaner.shutil.move
        moved = []
        os.rename = lambda *a, **k: (_ for _ in ()).throw(
            OSError(errno.EXDEV, "Cross-device link"))
        disk_cleaner.shutil.move = lambda s, d: moved.append((s, d))
        try:
            disk_cleaner.move_to_trash(src, trash_dir=trash)
        finally:
            os.rename, disk_cleaner.shutil.move = orig_rename, orig_move
        self.assertEqual(len(moved), 1)  # copied across volume


class ScanChildrenTest(unittest.TestCase):
    def test_lists_each_child_as_item(self):
        parent = tempfile.mkdtemp()
        a = os.path.join(parent, "appA")
        os.makedirs(a)
        with open(os.path.join(a, "c.bin"), "wb") as f:
            f.write(b"x" * 300)
        b = os.path.join(parent, "appB")
        os.makedirs(b)
        with open(os.path.join(b, "c.bin"), "wb") as f:
            f.write(b"y" * 100)
        os.makedirs(os.path.join(parent, "empty"))  # 0 bytes -> skipped

        items = disk_cleaner.scan_children(
            [parent], "system_cache", True)

        labels = sorted(i["label"] for i in items)
        self.assertEqual(labels, ["appA", "appB"])
        sizes = {i["label"]: i["size"] for i in items}
        self.assertEqual(sizes["appA"], 300)
        self.assertEqual(sizes["appB"], 100)
        self.assertTrue(all(i["category"] == "system_cache" for i in items))
        self.assertTrue(all(i["default_checked"] for i in items))

    def test_missing_parent_skipped(self):
        self.assertEqual(
            disk_cleaner.scan_children(["/no/such/dir"], "system_cache", True),
            [])


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
        stub_item = {"path": "/x", "size": 10, "category": "c",
                     "label": "x", "default_checked": True}
        inv = disk_cleaner.build_inventory(
            scan_paths=lambda paths, category, default_checked: [stub_item],
            scan_children=lambda parents, category, default_checked: [stub_item],
            scan_large_files=lambda root: [],
            scan_duplicates=lambda roots: [],
            brew_cache=lambda: [],
            disk_usage=lambda p: type("U", (), {"free": 50, "total": 100})(),
        )
        keys = {c["key"] for c in inv["categories"]}
        self.assertEqual(
            keys,
            {"system_cache", "dev_cache", "large_files", "duplicates"},
        )
        for c in inv["categories"]:
            self.assertIn("items", c)
            self.assertIn("size", c)
        self.assertEqual(inv["disk"], {"free": 50, "total": 100})


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

    def test_iter_delete_continues_past_permission_failure(self):
        root = tempfile.mkdtemp()
        ok_path = os.path.join(root, "ok.bin")
        with open(ok_path, "wb") as f:
            f.write(b"x" * 500)
        bad_path = os.path.join(root, "bad.bin")
        with open(bad_path, "wb") as f:
            f.write(b"y" * 999)

        def mover(path):
            if path == bad_path:
                raise PermissionError("Operation not permitted")

        events = list(disk_cleaner.iter_delete(
            [bad_path, ok_path], mover=mover))

        # both processed despite the first one failing
        self.assertEqual(len(events), 2)
        self.assertFalse(events[0]["ok"])
        self.assertIn("not permitted", events[0]["reason"])
        self.assertTrue(events[1]["ok"])
        # freed reflects only the successful move
        self.assertEqual(events[1]["freed"], 500)


class RenderHtmlTest(unittest.TestCase):
    def test_embeds_data_and_categories(self):
        inv = {
            "categories": [
                {"key": "system_cache", "title": "시스템 캐시/로그",
                 "size": 1000,
                 "items": [{"path": "/tmp/a", "size": 1000,
                            "category": "system_cache", "label": "a",
                            "default_checked": True}]},
            ],
            "disk": {"free": 50, "total": 100},
        }
        html = disk_cleaner.render_html(inv)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("시스템 캐시/로그", html)
        self.assertIn("/tmp/a", html)
        self.assertIn("/delete", html)

    def test_percent_in_path_does_not_crash(self):
        inv = {
            "categories": [
                {"key": "system_cache", "title": "t", "size": 5,
                 "items": [{"path": "/tmp/cache-100%-x.bin", "size": 5,
                            "category": "system_cache", "label": "c",
                            "default_checked": True}]},
            ],
            "disk": {"free": 1, "total": 2},
        }
        html = disk_cleaner.render_html(inv)  # must not raise
        self.assertIn("/tmp/cache-100%-x.bin", html)

    def test_js_newline_literal_not_interpreted(self):
        # PAGE_TEMPLATE is a raw string: the JS token indexOf('\n') must reach
        # the browser as backslash-n, not an actual newline (which breaks JS).
        html = disk_cleaner.render_html(disk_cleaner._INVENTORY)
        self.assertIn(r"indexOf('\n')", html)
        # and there must be no real newline inside that JS string literal
        self.assertNotIn("indexOf('\n')", html)


class ServerTest(unittest.TestCase):
    def setUp(self):
        self._orig_inv = disk_cleaner._INVENTORY
        self._orig_iter = disk_cleaner.iter_delete
        disk_cleaner._INVENTORY = {
            "categories": [{"key": "system_cache", "title": "t", "size": 0,
                            "items": []}],
            "disk": {"free": 1, "total": 2},
        }
        self.calls = []

        def fake_iter(paths):
            self.calls.append(paths)
            for i, p in enumerate(paths, 1):
                yield {"i": i, "total": len(paths), "path": p,
                       "ok": True, "size": 10, "freed": 10 * i}

        disk_cleaner.iter_delete = fake_iter
        port = disk_cleaner.find_port()
        from http.server import ThreadingHTTPServer
        self.server = ThreadingHTTPServer(("127.0.0.1", port),
                                          disk_cleaner.CleanerHandler)
        self.port = port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        disk_cleaner._INVENTORY = self._orig_inv
        disk_cleaner.iter_delete = self._orig_iter

    def _conn(self):
        return http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)

    def test_get_root_serves_html(self):
        c = self._conn(); c.request("GET", "/")
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        self.assertIn(b"<!DOCTYPE html>", r.read())

    def test_get_unknown_404(self):
        c = self._conn(); c.request("GET", "/nope")
        self.assertEqual(c.getresponse().status, 404)

    def test_post_delete_streams_ndjson_progress(self):
        c = self._conn()
        body = json.dumps({"paths": ["/tmp/x", "/tmp/y"]})
        c.request("POST", "/delete", body=body,
                  headers={"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 200)
        lines = [ln for ln in r.read().decode().split("\n") if ln.strip()]
        events = [json.loads(ln) for ln in lines]
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["i"], 1)
        self.assertEqual(events[-1]["freed"], 20)
        self.assertTrue(all(e["ok"] for e in events))
        self.assertEqual(self.calls, [["/tmp/x", "/tmp/y"]])

    def test_post_bad_json_400(self):
        c = self._conn()
        c.request("POST", "/delete", body="{not json",
                  headers={"Content-Type": "application/json"})
        self.assertEqual(c.getresponse().status, 400)
