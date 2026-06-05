import hashlib
import os
import shutil
import subprocess


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


class ProtectedPathError(Exception):
    pass


def is_protected(path):
    """True if path is a protected root, or an ancestor/equal of one."""
    norm = os.path.normpath(os.path.abspath(path))
    for guard in PROTECTED:
        g = os.path.normpath(guard)
        if norm == g:
            return True
        if g.startswith(norm + os.sep):
            return True
    return False


def move_command(path):
    """Build the osascript argv that moves a path to Finder Trash."""
    escaped = path.replace("\\", "\\\\").replace('"', '\\"')
    script = (
        'tell application "Finder" to delete (POSIX file "%s" as alias)'
        % escaped
    )
    return ["osascript", "-e", script]


def move_to_trash(path):
    """Move a single path to Trash. Raises ProtectedPathError if guarded."""
    path = os.path.normpath(os.path.abspath(path))
    if is_protected(path):
        raise ProtectedPathError(path)
    subprocess.run(move_command(path), check=True,
                   capture_output=True, text=True)


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


def scan_large_files(root, threshold=500 * 1024 * 1024):
    """Find individual files of at least threshold bytes under root."""
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


def _sha256(path, chunk=1 << 20):
    """Return hex SHA-256 digest of the file at path."""
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
        if size == 0 or len(paths) < 2:
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
