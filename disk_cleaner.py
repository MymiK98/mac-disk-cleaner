import errno
import hashlib
import json
import os
import shutil
import socket
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


def move_to_trash(path, trash_dir=None):
    """Move a path into ~/.Trash by direct filesystem move.

    No Finder/osascript automation, so it never blocks on a TCC prompt.
    Raises ProtectedPathError if guarded; lets OSError (e.g. PermissionError)
    propagate so the caller can record it as a failure and continue.
    Returns the destination path.
    """
    path = os.path.normpath(os.path.abspath(path))
    if is_protected(path):
        raise ProtectedPathError(path)
    if trash_dir is None:
        trash_dir = os.path.join(HOME, ".Trash")
    os.makedirs(trash_dir, exist_ok=True)
    base = os.path.basename(path.rstrip(os.sep)) or "item"
    dest = os.path.join(trash_dir, base)
    if os.path.exists(dest):
        stem, ext = os.path.splitext(base)
        n = 1
        while os.path.exists(dest):
            dest = os.path.join(trash_dir, "%s %d%s" % (stem, n, ext))
            n += 1
    try:
        os.rename(path, dest)
    except OSError as exc:
        if exc.errno == errno.EXDEV:
            # genuinely on another volume -> copy+remove is the only option
            shutil.move(path, dest)
        else:
            # EPERM/EACCES (e.g. TCC-protected cache): do NOT copy -- that would
            # duplicate gigabytes and still fail. Report as a clean failure.
            raise
    return dest


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


def scan_children(parents, category, default_checked):
    """List each immediate child of every parent dir as its own item.

    Used for ~/Library/Caches etc. so one TCC-protected subfolder cannot block
    deletion of all the others, and each app's cache is selectable on its own.
    """
    items = []
    for parent in parents:
        if not os.path.isdir(parent):
            continue
        try:
            names = sorted(os.listdir(parent))
        except OSError:
            continue
        for name in names:
            p = os.path.join(parent, name)
            size = dir_size(p)
            if size == 0:
                continue
            items.append({
                "path": p,
                "size": size,
                "category": category,
                "label": name,
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
    scan_children=scan_children,
    scan_large_files=scan_large_files,
    scan_duplicates=scan_duplicates,
    brew_cache=_brew_cache,
    disk_usage=shutil.disk_usage,
    progress=None,
):
    def note(msg):
        if progress is not None:
            progress(msg)

    # System caches/logs: expand into per-subfolder items so one protected
    # entry (Safari/HomeKit/...) doesn't fail the whole parent. ~/.Trash is
    # excluded -- moving the trash into the trash makes no sense.
    cache_parents = [
        os.path.join(HOME, "Library", "Caches"),
        os.path.join(HOME, "Library", "Logs"),
    ]
    dev_paths = [
        os.path.join(HOME, ".npm"),
        os.path.join(HOME, ".cache"),
        os.path.join(HOME, "Library", "Developer", "Xcode", "DerivedData"),
        os.path.join(HOME, "Library", "Developer", "CoreSimulator", "Caches"),
    ] + brew_cache()

    note("[1/4] 시스템 캐시/로그 스캔...")
    system_items = scan_children(cache_parents, "system_cache", True)
    note("[2/4] 개발 캐시 스캔...")
    dev_items = scan_paths(dev_paths, "dev_cache", True)
    note("[3/4] 대용량 파일 스캔 (홈 디렉토리, 수십초 소요)...")
    large_items = scan_large_files(HOME)
    note("[4/4] 중복 파일 스캔 (다운로드)...")
    dup_items = scan_duplicates([os.path.join(HOME, "Downloads")])

    categories = [
        {"key": "system_cache", "title": "시스템 캐시/로그", "items": system_items},
        {"key": "dev_cache", "title": "개발 캐시", "items": dev_items},
        {"key": "large_files", "title": "대용량 파일", "items": large_items},
        {"key": "duplicates", "title": "중복/다운로드", "items": dup_items},
    ]
    for c in categories:
        c["size"] = sum(i["size"] for i in c["items"])

    note("스캔 완료")
    usage = disk_usage("/")
    return {
        "categories": categories,
        "disk": {"free": usage.free, "total": usage.total},
    }


def iter_delete(paths, mover=None):
    """Yield a progress event per path as it is moved to Trash.

    Each event: {i, total, path, ok, freed, and size or reason}. A failure on
    one path (e.g. PermissionError) is reported and the batch continues.
    """
    if mover is None:
        mover = move_to_trash
    freed = 0
    total = len(paths)
    for i, p in enumerate(paths, 1):
        size = dir_size(p)
        try:
            mover(p)
            freed += size
            yield {"i": i, "total": total, "path": p, "ok": True,
                   "size": size, "freed": freed}
        except Exception as exc:  # noqa: BLE001 - report, never abort batch
            yield {"i": i, "total": total, "path": p, "ok": False,
                   "reason": str(exc), "freed": freed}


def delete_paths(paths, mover=None):
    """Move each path to Trash. Returns freed bytes and failure list."""
    freed = 0
    failed = []
    for ev in iter_delete(paths, mover=mover):
        freed = ev["freed"]
        if not ev["ok"]:
            failed.append({"path": ev["path"], "reason": ev["reason"]})
    return {"freed": freed, "failed": failed}


# raw string: JS escape sequences like '\n' must reach the browser verbatim,
# not be interpreted by Python.
PAGE_TEMPLATE = r"""<!DOCTYPE html>
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
 .overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);
   align-items:center;justify-content:center;z-index:10}
 .overlay.show{display:flex}
 .panel{background:#fff;border-radius:12px;padding:24px;width:560px;max-width:90vw;
   max-height:80vh;display:flex;flex-direction:column}
 .panel h2{margin:0 0 12px;font-size:16px}
 .bar{height:10px;background:#e8e8ed;border-radius:5px;overflow:hidden;margin:8px 0}
 .bar > div{height:100%;width:0;background:#0071e3;transition:width .15s}
 .prog-text{font-size:13px;color:#86868b;margin-bottom:4px}
 .cur{font-size:12px;color:#86868b;word-break:break-all;min-height:16px}
 .fails{margin-top:12px;overflow:auto;font-size:12px;color:#c0392b;flex:1}
 .fails div{padding:3px 0;border-top:1px solid #f0f0f0;word-break:break-all}
 .panel .close{display:none;margin-top:16px;align-self:flex-end}
</style></head><body>
<header>
 <div><h1>🧹 Mac 용량정리</h1><div id="disk"></div></div>
 <div><span class="sel" id="selected">선택됨: 0 B</span>
  <button class="ghost" onclick="location.reload()">재스캔</button></div>
</header>
<main id="app"></main>
<footer><button onclick="confirmDelete()">선택항목 휴지통 이동 →</button></footer>
<div class="overlay" id="overlay"><div class="panel">
 <h2 id="prog-title">휴지통으로 이동 중...</h2>
 <div class="prog-text" id="prog-count">0 / 0</div>
 <div class="bar"><div id="prog-fill"></div></div>
 <div class="prog-text" id="prog-freed">확보: 0 B</div>
 <div class="cur" id="prog-cur"></div>
 <div class="fails" id="prog-fails"></div>
 <button class="close" id="prog-close" onclick="location.reload()">닫기</button>
</div></div>
<script>
const DATA = __DATA__;
function fmt(b){const u=['B','KB','MB','GB','TB'];let i=0,n=b;
 while(n>=1024&&i<u.length-1){n/=1024;i++;}return n.toFixed(i?1:0)+' '+u[i];}
function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function syncCat(catBox,items){const boxes=items.querySelectorAll('input');
 const n=[...boxes].filter(b=>b.checked).length;
 catBox.checked=boxes.length>0&&n===boxes.length;
 catBox.indeterminate=n>0&&n<boxes.length;}
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
   row.querySelector('input').onchange=()=>{syncCat(catBox,items);update();};
   items.appendChild(row);});
  syncCat(catBox,items);
  cat.appendChild(head);cat.appendChild(items);app.appendChild(cat);});
 update();}
function selected(){return [...document.querySelectorAll('input[data-path]')]
 .filter(b=>b.checked);}
function update(){const s=selected().reduce((a,b)=>a+(+b.dataset.size),0);
 document.getElementById('selected').textContent='선택됨: '+fmt(s);}
function setProg(i,total,freed,cur){
 document.getElementById('prog-count').textContent=i+' / '+total;
 document.getElementById('prog-fill').style.width=
  (total?Math.round(i/total*100):0)+'%';
 document.getElementById('prog-freed').textContent='확보: '+fmt(freed);
 document.getElementById('prog-cur').textContent=cur;}
async function confirmDelete(){const sel=selected();
 if(!sel.length){alert('선택된 항목이 없습니다.');return;}
 const total=sel.reduce((a,b)=>a+(+b.dataset.size),0);
 if(!confirm(sel.length+'개 / '+fmt(total)+' 휴지통으로 이동할까요?'))return;
 const paths=sel.map(b=>b.dataset.path);
 const ov=document.getElementById('overlay');ov.classList.add('show');
 document.getElementById('prog-title').textContent='휴지통으로 이동 중...';
 document.getElementById('prog-fails').innerHTML='';
 setProg(0,paths.length,0,'');
 let resp;
 try{resp=await fetch('/delete',{method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({paths})});}
 catch(e){document.getElementById('prog-title').textContent='네트워크 오류';
  document.getElementById('prog-close').style.display='inline-block';return;}
 const reader=resp.body.getReader(),dec=new TextDecoder();
 let buf='',fails=0,freed=0,doneN=0;
 while(true){
  const {value,done}=await reader.read();if(done)break;
  buf+=dec.decode(value,{stream:true});let nl;
  while((nl=buf.indexOf('\n'))>=0){
   const line=buf.slice(0,nl);buf=buf.slice(nl+1);
   if(!line.trim())continue;
   const ev=JSON.parse(line);doneN=ev.i;freed=ev.freed;
   setProg(ev.i,ev.total,ev.freed,(ev.ok?'✓ ':'✗ ')+ev.path);
   if(!ev.ok){fails++;const d=document.createElement('div');
    d.textContent='실패: '+ev.path+' ('+ev.reason+')';
    document.getElementById('prog-fails').appendChild(d);}}}
 document.getElementById('prog-title').textContent=
  '완료 — '+fmt(freed)+' 확보, 실패 '+fails+'건';
 document.getElementById('prog-cur').textContent='';
 if(fails>0){const h=document.createElement('div');h.style.color='#86868b';
  h.textContent='💡 권한 거부 항목은 시스템 설정 → 개인정보 보호 및 보안 → '+
   '전체 디스크 접근 권한에 터미널(또는 Python)을 추가하면 삭제됩니다.';
  const fl=document.getElementById('prog-fails');fl.insertBefore(h,fl.firstChild);}
 document.getElementById('prog-close').style.display='inline-block';}
render();
</script></body></html>"""


def render_html(inventory):
    data_json = json.dumps(inventory, ensure_ascii=False)
    return PAGE_TEMPLATE.replace("__DATA__", data_json, 1)


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
        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (json.JSONDecodeError, ValueError):
            self._send(400, json.dumps({"error": "bad request"}),
                       "application/json; charset=utf-8")
            return
        paths = payload.get("paths", [])
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for ev in iter_delete(paths):
            line = (json.dumps(ev, ensure_ascii=False) + "\n").encode("utf-8")
            try:
                self.wfile.write(line)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                break

    def log_message(self, *args):
        pass


def find_port(start=8765):
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("no free port")


def main():
    global _INVENTORY
    print("스캔 중...")
    _INVENTORY = build_inventory(progress=lambda m: print(m, flush=True))
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
