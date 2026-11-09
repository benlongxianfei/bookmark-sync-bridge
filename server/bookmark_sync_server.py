"""
Bookmark Sync Bridge - local service
------------------------------------
Listens on 127.0.0.1 only. Receives bookmark trees pushed by the browser
extension, merges them into a master tree, archives them, and serves them
to other browsers.

API:
  GET  /health   health check
  GET  /pull     get master tree
  POST /push     {"browser":"chrome","role":"master","tree":{...}}
  GET  /stats    last push per browser

Configuration: place config.json next to this file (see config.example.json).
No hardcoded machine paths: bookmark files are auto-discovered, or you can
list them explicitly in config.json.
"""
import json, os, io, time, glob, threading, datetime
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(os.path.dirname(ROOT), "data")
BK   = os.path.join(DATA, "backups")
MASTER_PATH = os.path.join(DATA, "master.json")
LOG_PATH = os.path.join(DATA, "server.log")
CONFIG_PATH = os.path.join(ROOT, "config.json")

DEFAULTS = {
    "port": 8765,
    "keep_days": 14,
    "guard_interval": 4,
    "auto_discover": True,
    "guard_targets": [],
}


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_PATH):
        try:
            user = json.load(io.open(CONFIG_PATH, "r", encoding="utf-8"))
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if k in DEFAULTS})
        except Exception as e:
            print("[warn] config.json parse failed, using defaults:", e)
    return cfg


CFG = load_config()
PORT = int(CFG.get("port", 8765))
KEEP_DAYS = int(CFG.get("keep_days", 14))


def discover_bookmark_files():
    """Auto-discover Chromium-family Bookmarks files in standard locations."""
    patterns = []
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA", "")
        if local:
            vendors = [
                ("Google", "Chrome"),
                ("Microsoft", "Edge"),
                ("Chromium", None),
                ("BraveSoftware", "Brave-Browser"),
                ("Vivaldi", None),
            ]
            for vendor, product in vendors:
                parts = [local, vendor]
                if product:
                    parts.append(product)
                parts.append("User Data")
                base = os.path.join(*parts)
                patterns.append(os.path.join(base, "*", "Bookmarks"))
    else:
        home = os.path.expanduser("~")
        for d in ("google-chrome", "chromium", "microsoft-edge"):
            patterns.append(os.path.join(home, ".config", d, "*", "Bookmarks"))
    found = []
    for pat in patterns:
        for f in glob.glob(pat):
            if os.path.isfile(f) and os.path.basename(f) == "Bookmarks":
                found.append(f)
    return sorted(set(found))


os.makedirs(BK, exist_ok=True)
LOCK = threading.Lock()

def log(msg):
    line = "%s  %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with io.open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

# ---------------- 书签树工具 ----------------
def flatten(node, path=None, out=None):
    """摊平成 {url: (name, [路径])}"""
    if out is None: out = {}
    if path is None: path = []
    for c in (node.get("children") or []):
        if c.get("type") == "folder":
            flatten(c, path + [c.get("name") or ""], out)
        elif c.get("type") == "url" and c.get("url"):
            out.setdefault(c["url"], (c.get("name") or "", list(path)))
    return out

def all_urls(tree):
    out = {}
    for k in ("bookmark_bar", "other", "synced"):
        r = (tree.get("roots") or {}).get(k)
        if r: out.update(flatten(r))
    return out

def find_folder(root, path):
    cur = root
    for name in path:
        nxt = None
        for c in (cur.get("children") or []):
            if c.get("type") == "folder" and (c.get("name") or "") == name:
                nxt = c; break
        if nxt is None:
            nxt = {"type": "folder", "name": name, "children": []}
            cur.setdefault("children", []).append(nxt)
        cur = nxt
    return cur

EMPTY_MASTER = {"roots": {
    "bookmark_bar": {"type": "folder", "name": "书签栏", "children": []},
    "other": {"type": "folder", "name": "其他书签", "children": []},
    "synced": {"type": "folder", "name": "移动设备书签", "children": []},
}, "version": 1}

STATE = {"master": None, "last": {}, "push_count": 0}

def load_master():
    if os.path.exists(MASTER_PATH):
        try:
            return json.load(io.open(MASTER_PATH, "r", encoding="utf-8"))
        except Exception as e:
            log("master.json 解析失败(%s)，改用空树" % e)
    return json.loads(json.dumps(EMPTY_MASTER))

def save_master(tree):
    tmp = MASTER_PATH + ".tmp"
    io.open(tmp, "w", encoding="utf-8").write(json.dumps(tree, ensure_ascii=False))
    os.replace(tmp, MASTER_PATH)

def archive(browser, tree):
    """
    按【浏览器 + 日期】存档 —— 同一浏览器同一天只保留最新一份。
    好处：既保留"每天一个状态快照"用于回溯，又不会因为高频推送堆积海量文件。
    """
    day = datetime.datetime.now().strftime("%Y%m%d")
    p = os.path.join(BK, "%s_%s.json" % (day, browser))
    tmp = p + ".tmp"
    io.open(tmp, "w", encoding="utf-8").write(json.dumps(tree, ensure_ascii=False))
    os.replace(tmp, p)          # 原子覆盖，永远只有一份
    return p

def cleanup_old():
    cut = time.time() - KEEP_DAYS * 86400
    for f in os.listdir(BK):
        p = os.path.join(BK, f)
        try:
            if os.path.getmtime(p) < cut: os.remove(p)
        except Exception:
            pass

def merge(master, tree):
    """把 tree 中 master 没有的 url 并入 master（master 原有位置优先，只增不删）"""
    have = all_urls(master)
    added = []
    for k in ("bookmark_bar", "other", "synced"):
        src = (tree.get("roots") or {}).get(k)
        if not src: continue
        for url, (name, path) in flatten(src).items():
            if url in have: continue
            droot = master["roots"].setdefault(k, {"type": "folder", "name": k, "children": []})
            tgt = find_folder(droot, path) if path else droot
            tgt.setdefault("children", []).append({"type": "url", "name": name, "url": url})
            have[url] = (name, path)
            added.append(url)
    return added

# ---------------- HTTP ----------------
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *a):
        pass  # 静音，自己记日志

    def do_OPTIONS(self):
        self.send_response(204); self._cors(); self.end_headers()

    def do_GET(self):
        path = self.path.split("?")[0]
        with LOCK:
            if path == "/health":
                m = STATE["master"] or EMPTY_MASTER
                return self._json({"ok": True, "urls": len(all_urls(m)),
                                   "pushes": STATE["push_count"],
                                   "updated": STATE.get("updated")})
            if path == "/pull":
                m = STATE["master"] or EMPTY_MASTER
                return self._json({"ok": True, "tree": m, "urls": len(all_urls(m))})
            if path == "/stats":
                return self._json({"ok": True, "last": STATE["last"], "pushes": STATE["push_count"]})
        return self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        if path != "/push":
            return self._json({"ok": False, "error": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n)
            payload = json.loads(raw.decode("utf-8"))
            browser = str(payload.get("browser") or "unknown")
            role = str(payload.get("role") or "follower")     # master | follower
            tree = payload.get("tree") or {}
            if not isinstance(tree.get("roots"), dict):
                return self._json({"ok": False, "error": "tree.roots 缺失"}, 400)
        except Exception as e:
            return self._json({"ok": False, "error": "解析失败: %s" % e}, 400)

        with LOCK:
            if STATE["master"] is None:
                STATE["master"] = load_master()
            before = len(all_urls(STATE["master"]))
            archive(browser, tree)

            if role == "master":
                # A master push is authoritative: deletions and ordering propagate.
                new_n = len(all_urls(tree))
                if before > 0 and new_n < before * 0.7:
                    # 安全阀：数量骤降 >30%，判定为异常推送，拒绝替换，改用只增合并
                    log("Guard: master tree shrank %d -> %d, keep additive merge" % (before, new_n))
                    added = merge(STATE["master"], tree)
                else:
                    STATE["master"] = tree          # 完全替换（删除也会被同步出去）
                    added = []
            else:
                # 从浏览器：只增不删（手动推送不会误删主树）
                added = merge(STATE["master"], tree)

            save_master(STATE["master"])
            export_html_backup(STATE["master"])      # refresh the portable HTML backup
            after = len(all_urls(STATE["master"]))
            STATE["push_count"] += 1
            STATE["updated"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            STATE["last"][browser] = {"time": STATE["updated"], "urls": len(all_urls(tree))}
        log("push %-10s(%s) 收到 %4d 条 | 主树 %d -> %d (%s)" % (
            browser, role, len(all_urls(tree)), before, after,
            "完全替换" if role == "master" and not added else "新增 %d" % len(added)))
        return self._json({"ok": True, "added": len(added), "master_urls": after,
                           "replaced": role == "master"})

def master_to_html(tree):
    """
    把主树导出为【标准 Netscape 书签 HTML】—— 人类可读、任何浏览器都能直接导入。
    作为独立于程序的"最后一层保险"。
    """
    def esc(s):
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    L = ['<!DOCTYPE NETSCAPE-Bookmark-file-1>',
         '<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">',
         '<TITLE>Bookmarks</TITLE>', '<H1>Bookmarks</H1>', '<DL><p>']
    cnt = [0]

    def walk(node, depth):
        ind = "    " * depth
        for c in (node.get("children") or []):
            if c.get("type") == "folder" or c.get("children") is not None:
                L.append('%s<DT><H3>%s</H3>' % (ind, esc(c.get("name"))))
                L.append('%s<DL><p>' % ind)
                walk(c, depth + 1)
                L.append('%s</DL><p>' % ind)
            elif c.get("url"):
                cnt[0] += 1
                L.append('%s<DT><A HREF="%s">%s</A>' % (ind, esc(c["url"]), esc(c.get("name"))))

    names = {"bookmark_bar": "书签栏", "other": "其他书签", "synced": "移动设备书签"}
    for k in ("bookmark_bar", "other", "synced"):
        r = (tree.get("roots") or {}).get(k)
        if not r:
            continue
        L.append('    <DT><H3>%s</H3>' % names[k])
        L.append('    <DL><p>')
        walk(r, 2)
        L.append('    </DL><p>')
    L.append('</DL><p>')
    return "\n".join(L), cnt[0]


def export_html_backup(tree):
    """把主树写成 data\\书签汇总备份.html（每次推送后自动刷新）"""
    try:
        html, cnt = master_to_html(tree)
        hp = os.path.join(DATA, "书签汇总备份.html")
        tmp = hp + ".tmp"
        io.open(tmp, "w", encoding="utf-8").write(html)
        os.replace(tmp, hp)
        return cnt
    except Exception as e:
        log("HTML 备份导出失败: %s" % e)
        return 0


def main():
    STATE["master"] = load_master()
    cleanup_old()
    n = len(all_urls(STATE["master"]))
    log("=" * 60)
    log("书签同步服务启动  http://127.0.0.1:%d  (主树 %d 条)" % (PORT, n))
    log("数据目录: %s" % DATA)
    log("=" * 60)
    threading.Thread(target=guard_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("服务停止")

# ---------------- Bookmark file guard (independent fallback) ----------------
# Some Chromium forks clear their Bookmarks file on startup. The guard restores
# it from the master tree and removes duplicate entries if a file gets dirty.
# It only intervenes on anomalies; a healthy file is never touched.
_targets = list(CFG.get("guard_targets") or [])
if CFG.get("auto_discover", True):
    _targets += discover_bookmark_files()
GUARD_TARGETS = sorted(set(_targets))
GUARD_EVERY = int(CFG.get("guard_interval", 4))
_guard_stat = {}

def to_chromium(tree):
    """把主树转成 Chromium 可读的 Bookmarks 格式（补 id/guid/date_added，去 checksum）"""
    import uuid, random, time as _t
    now_us = int((_t.time() + 11644473600) * 1_000_000)
    out = json.loads(json.dumps(tree))
    out.pop("checksum", None)
    out["version"] = 1
    c = [1]
    def walk(n):
        for x in (n.get("children") or []):
            c[0] += 1
            x["id"] = str(c[0])
            x.setdefault("guid", str(uuid.uuid4()))
            d = str(x.get("date_added") or "")
            if not d.isdigit() or len(d) < 12:
                x["date_added"] = str(now_us - random.randint(0, 30) * 86400 * 1_000_000)
            if x.get("type") == "folder":
                x.setdefault("date_modified", "0"); walk(x)
            else:
                x["type"] = "url"; x.pop("date_modified", None)
    names = {"bookmark_bar": "书签栏", "other": "其他书签", "synced": "移动设备书签"}
    for k, nm in names.items():
        r = out["roots"].get(k)
        if r is None:
            r = {"children": [], "type": "folder"}; out["roots"][k] = r
        c[0] += 1
        r.update({"id": str(c[0]), "type": "folder", "name": nm,
                  "date_added": str(now_us - 400 * 86400 * 1_000_000),
                  "date_modified": "0"})
        r.setdefault("guid", str(uuid.uuid4()))
        walk(r)
    return out

def dedupe_tree(tree):
    """
    按 URL 去重（重建式）：
      - 同一位置重复 -> 只留 1 份
      - 跨位置重复   -> 保留路径最深（分类最具体）的那份
    返回 (去重后的树, 删除的条数)
    """
    # 1) 收集所有条目
    entries = []
    def collect(node, path, root):
        for c in (node.get("children") or []):
            if c.get("type") == "folder":
                collect(c, path + [c.get("name") or ""], root)
            elif c.get("type") == "url" and c.get("url"):
                entries.append({"url": c["url"], "name": c.get("name") or "",
                                "path": list(path), "root": root})
    roots = tree.get("roots") or {}
    for k in ("bookmark_bar", "other", "synced"):
        r = roots.get(k)
        if r: collect(r, [], k)

    by_url = {}
    for e in entries:
        by_url.setdefault(e["url"], []).append(e)

    def score(e):
        sp = sum(1 for s in e["path"] if s != s.strip())
        return (len(e["path"]), -sp)

    kept, dropped = [], 0
    for url, es in by_url.items():
        kept.append(max(es, key=score))
        dropped += len(es) - 1
    if dropped == 0:
        return tree, 0

    kept.sort(key=lambda e: (e["root"], e["path"]))

    # 2) 重建
    new = {"roots": {}, "version": 1}
    ROOTNAME = {"bookmark_bar": "书签栏", "other": "其他书签", "synced": "移动设备书签"}
    for k in ("bookmark_bar", "other", "synced"):
        root = {"type": "folder", "name": ROOTNAME[k], "children": []}
        new["roots"][k] = root
        idx = {(): root}
        for e in kept:
            if e["root"] != k: continue
            cur, key = root, ()
            for seg in e["path"]:
                key = key + (seg,)
                nxt = idx.get(key)
                if nxt is None:
                    nxt = {"type": "folder", "name": seg, "children": []}
                    cur.setdefault("children", []).append(nxt)
                    idx[key] = nxt
                cur = nxt
            cur.setdefault("children", []).append({"type": "url", "name": e["name"], "url": e["url"]})
    return new, dropped


def count_dupes(tree):
    """返回 (节点数, 唯一url数, 重复url数, 多余条数)"""
    seen, dup, tot = set(), 0, 0
    def w(n):
        nonlocal dup, tot
        for c in (n.get("children") or []):
            if c.get("type") == "folder": w(c)
            elif c.get("type") == "url" and c.get("url"):
                tot += 1
                if c["url"] in seen: dup += 1
                else: seen.add(c["url"])
    for k in ("bookmark_bar", "other", "synced"):
        r = (tree.get("roots") or {}).get(k)
        if r: w(r)
    return tot, len(seen), dup, dup


def guard_loop():
    log("书签守护已启动，看护 %d 个文件，每 %d 秒检查" % (len(GUARD_TARGETS), GUARD_EVERY))
    while True:
        try:
            time.sleep(GUARD_EVERY)
            for path in GUARD_TARGETS:
                d = os.path.dirname(path)
                if not os.path.isdir(d):
                    continue

                # ---------- A) 文件【完全不存在】-> 从主树恢复 ----------
                # Only a missing file is considered an anomaly. An empty local
                # Bookmarks file is legitimate when account bookmarks are used.
                if not os.path.exists(path):
                    with LOCK:
                        master = STATE["master"] or load_master()
                    if not all_urls(master):
                        continue
                    data = to_chromium(master)
                    tmp = path + ".guardtmp"
                    io.open(tmp, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))
                    os.replace(tmp, path)
                    log("Guard: missing bookmark file restored %d entries -> %s" % (
                        len(all_urls(master)), os.path.basename(path)))
                    continue

                # ---------- B) 文件存在 -> 检查重复，有则自动去重 ----------
                try:
                    raw = io.open(path, "r", encoding="utf-8", errors="ignore").read()
                    cur = json.loads(raw)
                except Exception:
                    continue          # 被占用/解析失败，跳过（下轮再试）
                if not isinstance(cur.get("roots"), dict):
                    continue
                tot, uniq, dups, extra = count_dupes(cur)
                if dups == 0:
                    continue
                fixed, dropped = dedupe_tree(cur)
                if dropped <= 0:
                    continue
                data = to_chromium(fixed)
                tmp = path + ".guardtmp"
                io.open(tmp, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))
                os.replace(tmp, path)
                bak = path + ".bak"
                try:
                    io.open(bak, "w", encoding="utf-8").write(json.dumps(data, ensure_ascii=False))
                except Exception:
                    pass
                log("Guard: removed %d duplicate entries -> %s (%d -> %d)" % (
                    dropped, os.path.basename(path), tot, tot - dropped))
        except Exception as e:
            log("守护线程异常: %s" % e)

if __name__ == "__main__":
    main()
