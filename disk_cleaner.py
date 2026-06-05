import os
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
