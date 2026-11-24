#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 提问捕获 + 防遗忘卡片 + 日报周报 + Obsidian 同步
单进程本地服务，只依赖 Python 标准库。

    python app.py              启动服务（含剪贴板旁路、定时任务）
    python app.py --no-watch   不监听剪贴板
    python app.py --no-sched   不跑定时任务
"""

import ctypes
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import paths
import store
import attach as attach_mod
from classify import score_text

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE_DIR = paths.DATA_DIR
DASH_PATH = paths.find("dashboard.html")
PORT = int(os.environ.get("AICARDS_PORT") or 8765)


def _port_alive(port, timeout=0.4):
    """本机端口是否已有服务在监听。"""
    import socket as _s
    s = _s.socket()
    s.settimeout(timeout)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass

# ---------------------------------------------------------------- 窗口与剪贴板

SRC_MAP = [
    ("豆包", "豆包"), ("doubao", "豆包"),
    ("deepseek", "DeepSeek"),
    ("kimi", "Kimi"),
    ("chatgpt", "ChatGPT"), ("openai", "ChatGPT"),
    ("claude", "Claude"),
    ("通义", "通义千问"), ("qwen", "通义千问"),
    ("文心", "文心一言"), ("yiyan", "文心一言"),
    ("智谱", "智谱"), ("zhipu", "智谱"),
    ("workbuddy", "WorkBuddy"),
    ("cursor", "Cursor"), ("copilot", "Copilot"),
]


def foreground_title():
    if os.name != "nt":
        return ""
    try:
        u = ctypes.windll.user32
        h = u.GetForegroundWindow()
        n = u.GetWindowTextLengthW(h)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        u.GetWindowTextW(h, buf, n + 1)
        return buf.value
    except Exception:
        return ""


def detect_source(title):
    low = (title or "").lower()
    for key, name in SRC_MAP:
        if key.lower() in low:
            return name
    return ""


def get_clipboard():
    if os.name != "nt":
        return ""
    try:
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        u.GetClipboardData.restype = ctypes.c_void_p
        k.GlobalLock.restype = ctypes.c_void_p
        k.GlobalLock.argtypes = [ctypes.c_void_p]
        k.GlobalUnlock.argtypes = [ctypes.c_void_p]
        if not u.OpenClipboard(None):
            return ""
        try:
            h = u.GetClipboardData(13)
            if not h:
                return ""
            p = k.GlobalLock(h)
            if not p:
                return ""
            try:
                return ctypes.wstring_at(p)
            finally:
                k.GlobalUnlock(h)
        finally:
            u.CloseClipboard()
    except Exception:
        return ""


Q_WORDS = ("怎么", "为什么", "如何", "什么", "能否", "可以吗", "哪个", "多少", "是不是", "区别")


def looks_like_question(t):
    t = (t or "").strip()
    return t.endswith(("?", "？", "吗")) or any(w in t for w in Q_WORDS)


def watcher_loop(stop, interval=1.2):
    last = ""
    print("[watch] clipboard bypass on")
    while not stop.is_set():
        try:
            text = get_clipboard()
            if text and text != last:
                last = text
                t = text.strip()
                if 6 <= len(t) <= 1500 and looks_like_question(t):
                    src = detect_source(foreground_title()) or "剪贴板"
                    card = store.add_card(t, src)
                    if card:
                        print("[watch] +card #%s [%s]" % (card["id"], card["weight"]))
        except Exception as e:
            print("[watch] err", e)
        stop.wait(interval)


# ---------------------------------------------------------------- 定时任务

DAY_KEYS = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def _due_at(now, spec):
    """spec 形如 '23:30' 或 'SUN 22:00'，判断现在是否已过该时刻。"""
    spec = (spec or "").strip().upper()
    if not spec:
        return False
    wd = None
    if " " in spec:
        wd_s, spec = spec.split(None, 1)
        wd = DAY_KEYS.get(wd_s)
        if wd is None:
            return False
    try:
        hh, mm = [int(x) for x in spec.split(":")]
    except Exception:
        return False
    if wd is not None and now.weekday() != wd:
        return False
    return (now.hour, now.minute) >= (hh, mm)


def scheduler_loop(stop, interval=45):
    import llm
    print("[sched] auto digest on")
    while not stop.is_set():
        try:
            cfg = llm.load_config()
            sched = cfg.get("schedule") or {}
            now = datetime.now()
            today = now.date().isoformat()

            if _due_at(now, sched.get("daily")) and store.job_last("daily") != today:
                if store.cards_between(today, today):
                    print("[sched] running daily digest for", today)
                    llm.run_daily(today, cfg=cfg)
                    print("[sched] daily done")

            week = "%d-W%02d" % now.isocalendar()[:2]
            if _due_at(now, sched.get("weekly")) and store.job_last("weekly") != week:
                if store.cards_between((now.date() - timedelta(days=6)).isoformat(), today):
                    print("[sched] running weekly digest", week)
                    llm.run_weekly(today, cfg=cfg)
                    print("[sched] weekly done")
        except Exception as e:
            print("[sched] err", e)
        stop.wait(interval)


# ---------------------------------------------------------------- 附件

MAX_FILE = 20 * 1024 * 1024     # 单个附件上限 20MB，超了就跳过（油猴那边也会先拦一次）
MAX_FILES = 8


def _pub(row):
    """回给前端的附件：不带全文，只带摘要，不然一轮对话就要传几百 KB。"""
    if not row:
        return None
    d = {k: row.get(k) for k in ("id", "card_id", "name", "mime", "kind", "size",
                                 "origin", "text_len")}
    d["snippet"] = attach_mod.snippet(row.get("text") or "")
    return d


def _save_uploads(cid, files, remotes):
    import base64
    out = []
    for f in (files or [])[:MAX_FILES]:
        raw = (f.get("data") or "").strip()
        if not raw:
            continue
        try:
            if raw.startswith("data:"):
                raw = raw.split(",", 1)[-1]
            buf = base64.b64decode(raw)
        except Exception:
            continue
        if not buf or len(buf) > MAX_FILE:
            continue
        meta = attach_mod.save_bytes(buf, f.get("name") or "附件", f.get("mime") or "", cid)
        row = store.add_attachment(cid, meta) if meta else None
        if row:
            out.append(_pub(row))
    for f in (remotes or [])[:MAX_FILES]:
        meta = attach_mod.save_remote(f.get("name"), f.get("url"), cid)
        row = store.add_attachment(cid, meta) if meta else None
        if row:
            out.append(_pub(row))
    return out


# ---------------------------------------------------------------- HTTP

def public_config(cfg):
    c = json.loads(json.dumps(cfg))
    c["api_key"] = "***set***" if (cfg.get("api_key") or "").strip() else ""
    return c


class Handler(BaseHTTPRequestHandler):
    server_version = "AICapture/2.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(204, b"")

    def do_GET(self):
        p = urlparse(self.path)
        path, qs = p.path, parse_qs(p.query)
        one = lambda k, d="": (qs.get(k) or [d])[0]
        try:
            if path in ("/", "/index.html", "/dashboard.html"):
                if not os.path.exists(DASH_PATH):
                    return self._send(500, "dashboard.html missing")
                with open(DASH_PATH, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/cards":
                store.sweep()
                return self._send(200, store.list_cards(one("view", "due"), one("q")))
            if path == "/api/stats":
                store.sweep()
                return self._send(200, store.stats())
            if path == "/api/config":
                import llm
                return self._send(200, public_config(llm.load_config()))
            if path == "/api/digests":
                return self._send(200, store.list_digests(one("kind") or None))
            if path == "/api/links":
                return self._send(200, store.all_links())
            if path == "/api/threads":
                return self._send(200, store.list_threads(one("q")))
            if path == "/api/thread":
                tid = one("id")
                th = store.get_thread(tid)
                if not th:
                    return self._send(404, {"error": "thread not found"})
                return self._send(200, {"thread": th, "turns": store.thread_tree(tid)})
            if path == "/api/file":
                aid = int(one("id") or 0)
                a = store.get_attachment(aid)
                if not a:
                    return self._send(404, {"error": "attachment not found"})
                if not a.get("path") and a.get("remote"):
                    # 只存了站点托管链接：直接跳过去，不在本地伪造一份
                    self.send_response(302)
                    self.send_header("Location", a["remote"])
                    self.end_headers()
                    return
                p = attach_mod.abs_path(a.get("path") or "")
                if not p:
                    return self._send(404, {"error": "file missing"})
                with open(p, "rb") as f:
                    data = f.read()
                ctype = a.get("mime") or "application/octet-stream"
                return self._send(200, data, ctype)
            if path == "/api/attach/text":
                aid = int(one("id") or 0)
                a = store.get_attachment(aid)
                if not a:
                    return self._send(404, {"error": "attachment not found"})
                return self._send(200, {"id": aid, "name": a.get("name"),
                                        "text_len": a.get("text_len"),
                                        "text": a.get("text") or ""})
            if path == "/api/loose":
                return self._send(200, store.loose_cards())
            if path == "/api/export":
                store.sweep()
                return self._send(200, store.list_cards("all"))
            return self._send(404, {"error": "not found"})
        except Exception as e:
            return self._send(500, {"error": str(e)})

    def do_POST(self):
        p = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return self._send(400, {"error": "bad json"})
        try:
            if p.path == "/api/capture":
                text = (data.get("text") or "").strip()
                files = data.get("files") or []
                remotes = data.get("remote_files") or []
                # 只发附件、一个字没打的提问也要留下，否则这一轮在库里完全消失
                if len(text) < 2 and (files or remotes):
                    names = [f.get("name") for f in files if f.get("name")]
                    names += [f.get("name") for f in remotes if f.get("name")]
                    text = "（只发了附件）" + "、".join(names[:3])
                card = store.add_card(
                    text,
                    data.get("source", "网页"),
                    data.get("url", ""),
                    data.get("weight"),
                    thread_id=data.get("conv") or "",
                    turn_no=data.get("turn") or None,
                    parent_id=data.get("parent") or None,
                )
                saved = []
                if card:
                    saved = _save_uploads(card["id"], files, remotes)
                    if saved:
                        card = store.get_card(card["id"])
                return self._send(200, {"ok": bool(card), "card": card, "files": saved})

            if p.path == "/api/attach":
                cid = int(data.get("card_id") or 0)
                if not cid:
                    return self._send(400, {"error": "card_id 不能为空"})
                saved = _save_uploads(cid, data.get("files") or [], data.get("remote_files") or [])
                return self._send(200, {"ok": bool(saved), "files": saved,
                                        "card": store.get_card(cid)})

            if p.path == "/api/attach/delete":
                return self._send(200, {"ok": store.delete_attachment(int(data["id"]))})

            if p.path == "/api/answer":
                import llm
                if not (llm.load_config().get("capture_answer", True)):
                    return self._send(200, {"ok": False, "ignored": "capture_answer off"})
                card = store.attach_answer(
                    data.get("prompt", ""),
                    data.get("answer", ""),
                    data.get("status", "ok"),
                    data.get("fmt", ""),
                    cid=data.get("id") or None,
                )
                return self._send(200, {"ok": bool(card), "card": card})

            if p.path == "/api/thread/rebind":
                return self._send(200, {"ok": store.rebind_thread(data.get("old", ""), data.get("new", ""))})

            if p.path == "/api/thread/title":
                tid, title = data.get("id", ""), (data.get("title") or "").strip()
                if not tid or not title:
                    return self._send(400, {"error": "id/title 不能为空"})
                return self._send(200, {"ok": store.set_thread_fields(tid, title=title[:60], title_auto=0)})

            if p.path == "/api/thread/archive":
                return self._send(200, {"ok": store.set_thread_fields(
                    data.get("id", ""), archived=int(data.get("flag", 1)))})

            if p.path == "/api/thread/split":
                th = store.split_thread(data.get("id", ""), data.get("turn") or 1)
                return self._send(200, {"ok": bool(th), "thread": th})

            if p.path == "/api/thread/make":
                th = store.make_thread(data.get("ids") or [], data.get("title") or "")
                return self._send(200, {"ok": bool(th), "thread": th})

            if p.path == "/api/thread/outline":
                import llm
                return self._send(200, llm.thread_outline(
                    data.get("id", ""), cfg=llm.load_config(), force=bool(data.get("force", True))))

            if p.path == "/api/reparent":
                cid = int(data["id"])
                pid = data.get("parent")
                store.set_parent(cid, int(pid) if pid else None)
                return self._send(200, {"ok": True})

            if p.path == "/api/review":
                return self._send(200, store.review_card(int(data["id"]), int(data.get("grade", 2))))

            if p.path == "/api/weight":
                return self._send(200, {"ok": store.set_field(int(data["id"]), weight=data["weight"], manual=1)})

            if p.path == "/api/conclusion":
                return self._send(200, {"ok": store.set_field(int(data["id"]), conclusion=data.get("text", ""))})

            if p.path == "/api/archive":
                return self._send(200, {"ok": store.set_field(int(data["id"]), archived=int(data.get("flag", 1)))})

            if p.path == "/api/delete":
                return self._send(200, {"ok": store.delete_card(int(data["id"]))})

            if p.path == "/api/config":
                import llm
                cfg = llm.load_config()
                for k, v in (data or {}).items():
                    if k == "api_key" and (v == "***set***" or v == ""):
                        continue
                    if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                        cfg[k].update(v)
                    else:
                        cfg[k] = v
                llm.save_config(cfg)
                return self._send(200, {"ok": True, "config": public_config(cfg)})

            if p.path == "/api/run":
                import llm
                what = data.get("what", "daily")
                cfg = llm.load_config()
                if what == "weekly":
                    return self._send(200, llm.run_weekly(data.get("day"), cfg=cfg))
                if what == "export":
                    import obsidian
                    return self._send(200, obsidian.export_all(cfg=cfg, force=bool(data.get("force"))))
                return self._send(200, llm.run_daily(data.get("day"), cfg=cfg))

            return self._send(404, {"error": "not found"})
        except Exception as e:
            import traceback
            traceback.print_exc()
            return self._send(500, {"error": str(e)})


def _say(*lines):
    for ln in lines:
        print(ln, flush=True)


def _pause():
    try:
        input("按回车退出...")
    except Exception:
        pass


def _llm_key_ok():
    try:
        import llm as _l
        return bool(_l.active())
    except Exception:
        return False


def main():
    store.init_db()
    try:
        n = store.backfill_threads()
        if n:
            print("[thread] 把 %d 组同会话散卡归回了线索" % n)
    except Exception as e:
        print("[thread] backfill skipped:", e)
    stop = threading.Event()

    if "--no-watch" not in sys.argv and os.name == "nt":
        threading.Thread(target=watcher_loop, args=(stop,), daemon=True).start()
    if "--no-sched" not in sys.argv:
        threading.Thread(target=scheduler_loop, args=(stop,), daemon=True).start()

    port = PORT
    # Windows 的 SO_REUSEADDR 允许两个进程绑同一端口（请求随机分发），
    # 所以不能依赖 bind 失败来判断重复启动，必须主动探一次。
    if _port_alive(PORT):
        _say(
            "=" * 60,
            " 服务已经在运行了 —— 不用再启动一次。",
            " 直接打开浏览器： http://127.0.0.1:%d" % PORT,
            "",
            " 想重启的话：先关掉之前那个黑窗口（标题是「AI 卡片库」），",
            " 再双击桌面图标。当前这个窗口可以直接关掉。",
            "=" * 60,
        )
        _pause()
        return

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        _say(
            "=" * 60,
            " 端口 %d 无法绑定：%s" % (PORT, e),
            " 可能有别的程序占用了它。命令行里执行下面的命令可以看是谁：",
            "   netstat -ano | findstr %d" % PORT,
            "=" * 60,
        )
        _pause()
        return

    cfg_model = ""
    try:
        import llm as _llm
        _c = _llm.load_config()
        cfg_model = _c.get("model") or ""
    except Exception:
        _c = {}
    va = (_c.get("obsidian") or {}).get("vault") or ""

    _say(
        "=" * 60,
        "  AI 卡片库 已启动",
        "",
        "  看板地址 : http://127.0.0.1:%d" % port,
        "  数据文件 : %s" % os.path.join(BASE_DIR, "cards.db"),
        "  配置文件 : config.json",
        "",
        "  大模型   : %s" % ("已配置 " + cfg_model if _llm_key_ok() else "未配置（日报走规则汇总，也能用）"),
        "  Obsidian : %s" % (va if va else "未配置（不同步）"),
        "",
        "  >> 这个窗口不要关，关掉服务就停了。最小化即可。",
        "=" * 60,
    )

    if "--no-open" not in sys.argv:
        import webbrowser
        url = "http://127.0.0.1:%d" % port
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        srv.server_close()


if __name__ == "__main__":
    main()
