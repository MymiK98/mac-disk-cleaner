# Mac Disk Cleaner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** macOS 디스크 용량을 스캔해 카테고리별로 분류하고, 로컬 웹 GUI에서 체크박스로 선택한 항목을 휴지통으로 이동하는 단일 Python3 스크립트를 만든다.

**Architecture:** 단일 `disk_cleaner.py`가 스캔(파일 인벤토리 수집) → 로컬 HTTP 서버(127.0.0.1)로 웹 GUI 제공 → POST 요청 받아 `osascript`로 휴지통 이동. 표준 라이브러리만 사용, pip 의존성 0. 테스트는 stdlib `unittest`.

**Tech Stack:** Python 3 (stdlib: `http.server`, `os`, `pathlib`, `hashlib`, `json`, `subprocess`, `webbrowser`), macOS `osascript`, 바닐라 HTML/CSS/JS.

---

## File Structure

| 파일 | 책임 |
|---|---|
| `disk_cleaner.py` | 전체: 크기계산, 보호경로검사, 스캐너, 휴지통이동, HTTP서버, HTML |
| `test_disk_cleaner.py` | 더미 디렉토리 기반 단위 테스트 (실제 시스템 안 건드림) |
| `README.md` | 실행법 |

단일 스크립트로 충분한 규모(수백줄). 모듈 분리는 YAGNI. 함수 경계로 책임 분리.

테스트 실행 공통: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner -v`

---

## Task 1: 크기 계산 헬퍼 `dir_size`

**Files:**
- Create: `mac-disk-cleaner/disk_cleaner.py`
- Test: `mac-disk-cleaner/test_disk_cleaner.py`

- [ ] **Step 1: Write the failing test**

```python
# test_disk_cleaner.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.DirSizeTest -v`
Expected: FAIL — `AttributeError: module 'disk_cleaner' has no attribute 'dir_size'`

- [ ] **Step 3: Write minimal implementation**

```python
# disk_cleaner.py
import os


def dir_size(path):
    """Total bytes of a file or directory tree. Missing/denied -> skipped."""
    if not os.path.exists(path):
        return 0
    if os.path.isfile(path):
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                if not os.path.islink(fp):
                    total += os.path.getsize(fp)
            except OSError:
                continue
    return total
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.DirSizeTest -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
cd mac-disk-cleaner
git add disk_cleaner.py test_disk_cleaner.py
git commit -m "feat: add dir_size helper"
```

---

## Task 2: 보호경로 검사 `is_protected`

**Files:**
- Modify: `mac-disk-cleaner/disk_cleaner.py`
- Test: `mac-disk-cleaner/test_disk_cleaner.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_disk_cleaner.py
class ProtectedPathTest(unittest.TestCase):
    def test_system_roots_are_protected(self):
        for p in ["/System", "/Library", "/usr", "/bin", "/Applications"]:
            self.assertTrue(disk_cleaner.is_protected(p), p)

    def test_home_root_and_library_protected(self):
        home = os.path.expanduser("~")
        self.assertTrue(disk_cleaner.is_protected(home))
        self.assertTrue(disk_cleaner.is_protected(os.path.join(home, "Library")))

    def test_parent_of_protected_is_protected(self):
        # exact match or ancestor of a delete target -> refuse
        self.assertTrue(disk_cleaner.is_protected("/"))

    def test_cache_subfolder_not_protected(self):
        home = os.path.expanduser("~")
        target = os.path.join(home, "Library", "Caches", "com.apple.Safari")
        self.assertFalse(disk_cleaner.is_protected(target))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.ProtectedPathTest -v`
Expected: FAIL — `has no attribute 'is_protected'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to disk_cleaner.py
HOME = os.path.expanduser("~")

PROTECTED = {
    "/",
    "/System",
    "/Library",
    "/Applications",
    "/usr",
    "/bin",
    "/sbin",
    "/etc",
    "/var",
    HOME,
    os.path.join(HOME, "Library"),
    os.path.join(HOME, "Documents"),
    os.path.join(HOME, "Desktop"),
    os.path.join(HOME, "Downloads"),
}


def is_protected(path):
    """True if path is a protected root, or an ancestor/equal of one."""
    norm = os.path.normpath(os.path.abspath(path))
    for guard in PROTECTED:
        g = os.path.normpath(guard)
        if norm == g:
            return True
        # refuse if deleting an ancestor of a guarded dir
        if g.startswith(norm + os.sep):
            return True
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.ProtectedPathTest -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Commit**

```bash
cd mac-disk-cleaner
git add disk_cleaner.py test_disk_cleaner.py
git commit -m "feat: add protected path guard"
```

---

## Task 3: 휴지통 이동 `move_to_trash`

**Files:**
- Modify: `mac-disk-cleaner/disk_cleaner.py`
- Test: `mac-disk-cleaner/test_disk_cleaner.py`

`osascript`는 테스트에서 실제 호출하지 않음 — `move_command(path)`가 만드는 인자 리스트를 검증하고, 보호경로 거부만 직접 테스트.

- [ ] **Step 1: Write the failing test**

```python
# append to test_disk_cleaner.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.MoveToTrashTest -v`
Expected: FAIL — `has no attribute 'move_command'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to disk_cleaner.py
import subprocess


class ProtectedPathError(Exception):
    pass


def move_command(path):
    """Build the osascript argv that moves a path to Finder Trash."""
    script = (
        'tell application "Finder" to delete (POSIX file "%s" as alias)'
        % path.replace('"', '\\"')
    )
    return ["osascript", "-e", script]


def move_to_trash(path):
    """Move a single path to Trash. Raises ProtectedPathError if guarded."""
    if is_protected(path):
        raise ProtectedPathError(path)
    subprocess.run(move_command(path), check=True,
                   capture_output=True, text=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.MoveToTrashTest -v`
Expected: PASS (2 tests)

- [ ] **Step 5: Commit**

```bash
cd mac-disk-cleaner
git add disk_cleaner.py test_disk_cleaner.py
git commit -m "feat: add move_to_trash with protected-path guard"
```

---

## Task 4: 스캐너 — 디렉토리 후보 수집 `scan_paths`

여러 스캐너가 공유하는 헬퍼: 경로 목록을 받아 존재하는 것만 인벤토리 항목으로 변환.

**Files:**
- Modify: `mac-disk-cleaner/disk_cleaner.py`
- Test: `mac-disk-cleaner/test_disk_cleaner.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_disk_cleaner.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.ScanPathsTest -v`
Expected: FAIL — `has no attribute 'scan_paths'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to disk_cleaner.py
def scan_paths(paths, category, default_checked):
    """Turn a list of paths into inventory items, skipping missing ones."""
    items = []
    for p in paths:
        if not os.path.exists(p):
            continue
        size = dir_size(p)
        if size == 0:
            continue
        items.append({
            "path": p,
            "size": size,
            "category": category,
            "label": os.path.basename(p.rstrip(os.sep)) or p,
            "default_checked": default_checked,
        })
    return items
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.ScanPathsTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd mac-disk-cleaner
git add disk_cleaner.py test_disk_cleaner.py
git commit -m "feat: add scan_paths inventory helper"
```

---

## Task 5: 대용량 파일 스캐너 `scan_large_files`

**Files:**
- Modify: `mac-disk-cleaner/disk_cleaner.py`
- Test: `mac-disk-cleaner/test_disk_cleaner.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_disk_cleaner.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.LargeFilesTest -v`
Expected: FAIL — `has no attribute 'scan_large_files'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to disk_cleaner.py
def scan_large_files(root, threshold=500 * 1024 * 1024):
    """Find individual files larger than threshold bytes under root."""
    items = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                if os.path.islink(fp):
                    continue
                size = os.path.getsize(fp)
            except OSError:
                continue
            if size >= threshold:
                items.append({
                    "path": fp,
                    "size": size,
                    "category": "large_files",
                    "label": name,
                    "default_checked": False,
                })
    items.sort(key=lambda i: i["size"], reverse=True)
    return items
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.LargeFilesTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd mac-disk-cleaner
git add disk_cleaner.py test_disk_cleaner.py
git commit -m "feat: add large file scanner"
```

---

## Task 6: 중복 파일 스캐너 `scan_duplicates`

크기가 같은 파일끼리만 sha256 비교(성능). 각 해시그룹의 첫 파일은 원본으로 두고 나머지를 후보로.

**Files:**
- Modify: `mac-disk-cleaner/disk_cleaner.py`
- Test: `mac-disk-cleaner/test_disk_cleaner.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_disk_cleaner.py
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
        # exactly 2 of the 3 identical files flagged; unique never flagged
        self.assertEqual(len(items), 2)
        self.assertNotIn("unique.bin", paths)
        self.assertTrue(all(i["category"] == "duplicates" for i in items))
        self.assertTrue(all(not i["default_checked"] for i in items))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.DuplicatesTest -v`
Expected: FAIL — `has no attribute 'scan_duplicates'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to disk_cleaner.py
import hashlib


def _sha256(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def scan_duplicates(roots):
    """Flag duplicate file copies (keep first seen as original)."""
    by_size = {}
    for root in roots:
        if not os.path.exists(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            for name in filenames:
                fp = os.path.join(dirpath, name)
                try:
                    if os.path.islink(fp):
                        continue
                    size = os.path.getsize(fp)
                except OSError:
                    continue
                by_size.setdefault(size, []).append(fp)

    items = []
    for size, paths in by_size.items():
        if len(paths) < 2:
            continue
        seen_hashes = {}
        for fp in sorted(paths):
            try:
                digest = _sha256(fp)
            except OSError:
                continue
            if digest in seen_hashes:
                items.append({
                    "path": fp,
                    "size": size,
                    "category": "duplicates",
                    "label": os.path.basename(fp),
                    "default_checked": False,
                })
            else:
                seen_hashes[digest] = fp
    return items
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.DuplicatesTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd mac-disk-cleaner
git add disk_cleaner.py test_disk_cleaner.py
git commit -m "feat: add duplicate file scanner"
```

---

## Task 7: 전체 인벤토리 조립 `build_inventory`

실제 macOS 경로를 묶어 4개 카테고리 전부 스캔. 테스트는 구조(카테고리 키 존재)만 검증 — 실제 시스템 크기에 의존하지 않음.

**Files:**
- Modify: `mac-disk-cleaner/disk_cleaner.py`
- Test: `mac-disk-cleaner/test_disk_cleaner.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_disk_cleaner.py
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
        self.assertIn("disk", inv)  # free/total bytes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.InventoryTest -v`
Expected: FAIL — `has no attribute 'build_inventory'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to disk_cleaner.py
import shutil


def _brew_cache():
    try:
        out = subprocess.run(["brew", "--cache"], capture_output=True,
                             text=True, check=True)
        return [out.stdout.strip()]
    except (OSError, subprocess.CalledProcessError):
        return []


def build_inventory():
    system_paths = [
        os.path.join(HOME, "Library", "Caches"),
        os.path.join(HOME, "Library", "Logs"),
        os.path.join(HOME, ".Trash"),
    ]
    dev_paths = [
        os.path.join(HOME, ".npm"),
        os.path.join(HOME, ".cache"),
        os.path.join(HOME, "Library", "Developer", "Xcode", "DerivedData"),
        os.path.join(HOME, "Library", "Developer", "CoreSimulator", "Caches"),
    ] + _brew_cache()

    categories = [
        {"key": "system_cache", "title": "시스템 캐시/로그",
         "items": scan_paths(system_paths, "system_cache", True)},
        {"key": "dev_cache", "title": "개발 캐시",
         "items": scan_paths(dev_paths, "dev_cache", True)},
        {"key": "large_files", "title": "대용량 파일",
         "items": scan_large_files(HOME)},
        {"key": "duplicates", "title": "중복/다운로드",
         "items": scan_duplicates([os.path.join(HOME, "Downloads")])},
    ]
    for c in categories:
        c["size"] = sum(i["size"] for i in c["items"])

    usage = shutil.disk_usage("/")
    return {
        "categories": categories,
        "disk": {"free": usage.free, "total": usage.total},
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.InventoryTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd mac-disk-cleaner
git add disk_cleaner.py test_disk_cleaner.py
git commit -m "feat: assemble full inventory across categories"
```

---

## Task 8: 삭제 배치 처리 `delete_paths`

POST 핸들러가 호출할 함수. 경로 목록 받아 휴지통 이동, 확보용량/실패 집계.

**Files:**
- Modify: `mac-disk-cleaner/disk_cleaner.py`
- Test: `mac-disk-cleaner/test_disk_cleaner.py`

osascript 회피 위해 `mover` 함수를 주입 가능하게 설계(기본값 `move_to_trash`).

- [ ] **Step 1: Write the failing test**

```python
# append to test_disk_cleaner.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.DeletePathsTest -v`
Expected: FAIL — `has no attribute 'delete_paths'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to disk_cleaner.py
def delete_paths(paths, mover=None):
    """Move each path to Trash. Returns freed bytes and failure list."""
    if mover is None:
        mover = move_to_trash
    freed = 0
    failed = []
    for p in paths:
        size = dir_size(p)
        try:
            mover(p)
            freed += size
        except Exception as exc:  # noqa: BLE001 - report, never abort batch
            failed.append({"path": p, "reason": str(exc)})
    return {"freed": freed, "failed": failed}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.DeletePathsTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd mac-disk-cleaner
git add disk_cleaner.py test_disk_cleaner.py
git commit -m "feat: add delete_paths batch handler"
```

---

## Task 9: HTML 렌더링 `render_html`

인벤토리 dict → 완전한 HTML 문자열. JS는 데이터를 `<script>` 안 JSON으로 임베드.

**Files:**
- Modify: `mac-disk-cleaner/disk_cleaner.py`
- Test: `mac-disk-cleaner/test_disk_cleaner.py`

- [ ] **Step 1: Write the failing test**

```python
# append to test_disk_cleaner.py
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
        self.assertIn("/delete", html)  # POST endpoint referenced in JS
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.RenderHtmlTest -v`
Expected: FAIL — `has no attribute 'render_html'`

- [ ] **Step 3: Write minimal implementation**

```python
# add to disk_cleaner.py
import json

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="ko"><head><meta charset="utf-8">
<title>Mac 용량정리</title>
<style>
 body{font-family:-apple-system,system-ui,sans-serif;margin:0;background:#f5f5f7;color:#1d1d1f}
 header{position:sticky;top:0;background:#fff;padding:16px 24px;border-bottom:1px solid #ddd;
   display:flex;justify-content:space-between;align-items:center}
 h1{font-size:18px;margin:0}
 .sel{font-weight:600}
 main{padding:16px 24px;max-width:880px;margin:0 auto}
 .cat{background:#fff;border-radius:10px;margin-bottom:12px;overflow:hidden}
 .cat-head{display:flex;align-items:center;gap:10px;padding:14px 16px;cursor:pointer;font-weight:600}
 .cat-head .size{margin-left:auto;color:#86868b}
 .items{display:none;border-top:1px solid #eee}
 .items.open{display:block}
 .row{display:flex;align-items:center;gap:10px;padding:10px 16px 10px 40px;border-top:1px solid #f0f0f0}
 .row .size{margin-left:auto;color:#86868b}
 .path{font-size:12px;color:#86868b;word-break:break-all}
 button{background:#0071e3;color:#fff;border:0;border-radius:8px;padding:10px 18px;font-size:14px;cursor:pointer}
 button.ghost{background:#e8e8ed;color:#1d1d1f}
 footer{position:sticky;bottom:0;background:#fff;padding:14px 24px;border-top:1px solid #ddd;text-align:right}
</style></head><body>
<header>
 <div><h1>🧹 Mac 용량정리</h1><div id="disk"></div></div>
 <div><span class="sel" id="selected">선택됨: 0 B</span>
  <button class="ghost" onclick="location.reload()">재스캔</button></div>
</header>
<main id="app"></main>
<footer><button onclick="confirmDelete()">선택항목 휴지통 이동 →</button></footer>
<script>
const DATA = %s;
function fmt(b){const u=['B','KB','MB','GB','TB'];let i=0,n=b;
 while(n>=1024&&i<u.length-1){n/=1024;i++;}return n.toFixed(i?1:0)+' '+u[i];}
function render(){
 document.getElementById('disk').textContent =
  '디스크: '+fmt(DATA.disk.free)+' 빈공간 / '+fmt(DATA.disk.total);
 const app=document.getElementById('app');app.innerHTML='';
 DATA.categories.forEach((c,ci)=>{
  const cat=document.createElement('div');cat.className='cat';
  const head=document.createElement('div');head.className='cat-head';
  head.innerHTML='<span class="tri">▶</span>'+
   '<input type="checkbox" data-cat="'+ci+'">'+
   '<span>'+c.title+'</span><span class="size">'+fmt(c.size)+'</span>';
  const items=document.createElement('div');items.className='items';
  head.querySelector('.tri').onclick=e=>{e.stopPropagation();
   items.classList.toggle('open');
   e.target.textContent=items.classList.contains('open')?'▼':'▶';};
  head.onclick=e=>{if(e.target.tagName==='INPUT')return;
   items.classList.toggle('open');
   head.querySelector('.tri').textContent=items.classList.contains('open')?'▼':'▶';};
  const catBox=head.querySelector('input');
  catBox.onchange=()=>{items.querySelectorAll('input').forEach(b=>b.checked=catBox.checked);update();};
  c.items.forEach((it,ii)=>{
   const row=document.createElement('div');row.className='row';
   row.innerHTML='<input type="checkbox" data-ci="'+ci+'" data-size="'+it.size+'"'+
    (it.default_checked?' checked':'')+'>'+
    '<div><div>'+it.label+'</div><div class="path">'+it.path+'</div></div>'+
    '<span class="size">'+fmt(it.size)+'</span>';
   row.querySelector('input').dataset.path=it.path;
   row.querySelector('input').onchange=update;
   items.appendChild(row);});
  if(c.items.some(it=>it.default_checked)){catBox.checked=
   c.items.every(it=>it.default_checked);}
  cat.appendChild(head);cat.appendChild(items);app.appendChild(cat);});
 update();}
function selected(){return [...document.querySelectorAll('input[data-path]')]
 .filter(b=>b.checked);}
function update(){const s=selected().reduce((a,b)=>a+(+b.dataset.size),0);
 document.getElementById('selected').textContent='선택됨: '+fmt(s);}
function confirmDelete(){const sel=selected();
 if(!sel.length){alert('선택된 항목이 없습니다.');return;}
 const total=sel.reduce((a,b)=>a+(+b.dataset.size),0);
 if(!confirm(sel.length+'개 / '+fmt(total)+' 휴지통으로 이동할까요?'))return;
 fetch('/delete',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({paths:sel.map(b=>b.dataset.path)})})
 .then(r=>r.json()).then(res=>{
  alert('완료: '+fmt(res.freed)+' 확보. 실패 '+res.failed.length+'건.');
  location.reload();});}
render();
</script></body></html>"""


def render_html(inventory):
    return PAGE_TEMPLATE % json.dumps(inventory, ensure_ascii=False)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner.RenderHtmlTest -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
cd mac-disk-cleaner
git add disk_cleaner.py test_disk_cleaner.py
git commit -m "feat: add HTML rendering for web GUI"
```

---

## Task 10: HTTP 서버 + 진입점 `main`

`GET /` → HTML, `POST /delete` → delete_paths. 127.0.0.1 바인드, 브라우저 자동 오픈. 서버는 테스트 안 함(진입점) — 핸들러 로직은 이미 분리된 함수로 테스트됨.

**Files:**
- Modify: `mac-disk-cleaner/disk_cleaner.py`

- [ ] **Step 1: 핸들러 + main 구현**

```python
# add to disk_cleaner.py
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_INVENTORY = {"categories": [], "disk": {"free": 0, "total": 0}}


class CleanerHandler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/":
            self._send(200, render_html(_INVENTORY))
        else:
            self._send(404, "not found")

    def do_POST(self):
        if self.path != "/delete":
            self._send(404, "not found")
            return
        length = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(length) or b"{}")
        result = delete_paths(payload.get("paths", []))
        self._send(200, json.dumps(result, ensure_ascii=False),
                   "application/json; charset=utf-8")

    def log_message(self, *args):
        pass  # quiet


def find_port(start=8765):
    import socket
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free port")


def main():
    global _INVENTORY
    print("스캔 중...")
    _INVENTORY = build_inventory()
    port = find_port()
    url = "http://127.0.0.1:%d/" % port
    server = ThreadingHTTPServer(("127.0.0.1", port), CleanerHandler)
    print("열림: %s  (Ctrl+C 종료)" % url)
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n종료.")
        server.shutdown()


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: 전체 테스트 통과 확인**

Run: `cd mac-disk-cleaner && python3 -m unittest test_disk_cleaner -v`
Expected: PASS (모든 클래스, 약 18개 테스트)

- [ ] **Step 3: 수동 스모크 테스트**

Run: `cd mac-disk-cleaner && python3 disk_cleaner.py`
Expected: "스캔 중..." → 브라우저에 카테고리/체크박스 페이지 열림. 항목 선택 → 휴지통 이동 모달 확인 동작. Ctrl+C로 종료.

- [ ] **Step 4: Commit**

```bash
cd mac-disk-cleaner
git add disk_cleaner.py
git commit -m "feat: add HTTP server and main entry point"
```

---

## Task 11: README

**Files:**
- Create: `mac-disk-cleaner/README.md`

- [ ] **Step 1: README 작성**

```markdown
# Mac Disk Cleaner

macOS 디스크 용량을 스캔해 카테고리별로 분류하고, 로컬 웹 GUI에서
체크박스로 선택한 항목을 **휴지통으로 이동**(되돌리기 가능)합니다.

## 요구사항
- macOS, Python 3 (기본 설치). 추가 설치 없음.

## 실행
```bash
python3 disk_cleaner.py
```
브라우저가 자동으로 열립니다. 항목 선택 후 "선택항목 휴지통 이동" 클릭.
종료는 터미널에서 Ctrl+C.

## 카테고리
- **시스템 캐시/로그** — `~/Library/Caches`, `~/Library/Logs`, 휴지통 (기본 선택)
- **개발 캐시** — npm, pip, brew, Xcode DerivedData 등 (기본 선택)
- **대용량 파일** — 홈 하위 500MB 이상 파일 (기본 해제)
- **중복/다운로드** — Downloads 내 중복 파일 (기본 해제)

## 안전
- 영구삭제 안 함. Finder 휴지통으로 이동 → 언제든 복원.
- 시스템 핵심 경로는 보호 목록으로 삭제 차단.
- 삭제 전 항목 수/용량 확인 모달.

## 테스트
```bash
python3 -m unittest test_disk_cleaner -v
```
```

- [ ] **Step 2: Commit**

```bash
cd mac-disk-cleaner
git add README.md
git commit -m "docs: add README"
```

---

## Self-Review

**Spec coverage:**
- 4개 카테고리 스캔 → Task 5,6,7 ✓
- 휴지통 이동(영구삭제 아님) → Task 3 ✓
- 보호경로 → Task 2 ✓
- 웹 GUI 체크박스 선택/카테고리 접기 → Task 9 ✓
- localhost 바인드 + 자동오픈 → Task 10 ✓
- 의존성 0 (stdlib only) → 전 Task `unittest`/stdlib ✓
- 에러처리(failed 집계, 중단 안함) → Task 8 ✓
- 테스트(더미 디렉토리) → 전 Task ✓
- README → Task 11 ✓

**Placeholder scan:** 모든 step에 실제 코드/명령 포함. 플레이스홀더 없음.

**Type consistency:** 인벤토리 항목 키(`path/size/category/label/default_checked`)
Task 4–9 전반 일치. 카테고리 dict 키(`key/title/size/items`) Task 7,9 일치.
`delete_paths`/`render_html`/`build_inventory` 시그니처 Task 10에서 호출과 일치.
