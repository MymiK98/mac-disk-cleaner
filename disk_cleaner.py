import hashlib
import json
import os
import shutil
import subprocess
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


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
    except (OSError, subprocess.CalledProcessError):
        return []
    path = out.stdout.strip()
    return [path] if path else []


def build_inventory(
    scan_paths=scan_paths,
    scan_large_files=scan_large_files,
    scan_duplicates=scan_duplicates,
    brew_cache=_brew_cache,
    disk_usage=shutil.disk_usage,
):
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
    ] + brew_cache()

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

    usage = disk_usage("/")
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
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function render(){
 document.getElementById('disk').textContent =
  '디스크: '+fmt(DATA.disk.free)+' 빈공간 / '+fmt(DATA.disk.total);
 const app=document.getElementById('app');app.innerHTML='';
 DATA.categories.forEach((c,ci)=>{
  const cat=document.createElement('div');cat.className='cat';
  const head=document.createElement('div');head.className='cat-head';
  head.innerHTML='<span class="tri">▶</span>'+
   '<input type="checkbox" data-cat="'+ci+'">'+
   '<span>'+esc(c.title)+'</span><span class="size">'+fmt(c.size)+'</span>';
  const items=document.createElement('div');items.className='items';
  head.querySelector('.tri').onclick=e=>{e.stopPropagation();
   items.classList.toggle('open');
   e.target.textContent=items.classList.contains('open')?'▼':'▶';};
  head.onclick=e=>{if(e.target.tagName==='INPUT')return;
   items.classList.toggle('open');
   head.querySelector('.tri').textContent=items.classList.contains('open')?'▼':'▶';};
  const catBox=head.querySelector('input');
  catBox.onchange=()=>{items.querySelectorAll('input').forEach(b=>b.checked=catBox.checked);update();};
  c.items.forEach((it)=>{
   const row=document.createElement('div');row.className='row';
   row.innerHTML='<input type="checkbox" data-size="'+it.size+'"'+
    (it.default_checked?' checked':'')+'>'+
    '<div><div>'+esc(it.label)+'</div><div class="path">'+esc(it.path)+'</div></div>'+
    '<span class="size">'+fmt(it.size)+'</span>';
   row.querySelector('input').dataset.path=it.path;
   row.querySelector('input').onchange=update;
   items.appendChild(row);});
  if(c.items.length){catBox.checked=c.items.every(it=>it.default_checked);}
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
        pass


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
