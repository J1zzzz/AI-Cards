#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据层：卡片、关联链、日报周报、任务状态。所有模块共用这一份。"""

import contextlib
import hashlib
import os
import re
import sqlite3
from datetime import datetime, timedelta

import paths

BASE_DIR = paths.DATA_DIR
DB_PATH = os.path.join(BASE_DIR, "cards.db")
FMT = "%Y-%m-%d %H:%M:%S"

CAP_DAYS = {"high": 3650, "mid": 180, "low": 30}
BASE_DAYS = {"high": 3, "mid": 2, "low": 1}


def now_str():
    return datetime.now().strftime(FMT)


def text_hash(text):
    return hashlib.sha1((text or "").strip().encode("utf-8")).hexdigest()


@contextlib.contextmanager
def db():
    c = sqlite3.connect(DB_PATH, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def _ensure_col(c, table, col, decl):
    cols = {r[1] for r in c.execute("PRAGMA table_info(%s)" % table)}
    if col not in cols:
        c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS cards(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              created_at TEXT NOT NULL,
              source TEXT DEFAULT '',
              url TEXT DEFAULT '',
              text TEXT NOT NULL,
              text_hash TEXT UNIQUE,
              score INTEGER DEFAULT 0,
              weight TEXT DEFAULT 'mid',
              manual INTEGER DEFAULT 0,
              conclusion TEXT DEFAULT '',
              tags TEXT DEFAULT '',
              review_count INTEGER DEFAULT 0,
              interval_days REAL DEFAULT 0,
              next_due TEXT,
              last_review TEXT,
              archived INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_due ON cards(archived, next_due);
            CREATE INDEX IF NOT EXISTS idx_created ON cards(created_at);

            CREATE TABLE IF NOT EXISTS links(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              a INTEGER NOT NULL, b INTEGER NOT NULL,
              relation TEXT DEFAULT '同主题',
              reason TEXT DEFAULT '',
              origin TEXT DEFAULT 'local',
              created_at TEXT,
              UNIQUE(a,b)
            );
            CREATE INDEX IF NOT EXISTS idx_link_a ON links(a);
            CREATE INDEX IF NOT EXISTS idx_link_b ON links(b);

            CREATE TABLE IF NOT EXISTS threads(
              id TEXT PRIMARY KEY,
              title TEXT DEFAULT '',
              intent TEXT DEFAULT '',
              source TEXT DEFAULT '',
              url TEXT DEFAULT '',
              created_at TEXT,
              updated_at TEXT,
              turns INTEGER DEFAULT 0,
              outline TEXT DEFAULT '',
              outline_at TEXT,
              title_auto INTEGER DEFAULT 1,
              archived INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_thread_up ON threads(archived, updated_at);

            CREATE TABLE IF NOT EXISTS digests(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              kind TEXT NOT NULL,
              period TEXT NOT NULL,
              created_at TEXT,
              content TEXT DEFAULT '',
              UNIQUE(kind,period)
            );

            CREATE TABLE IF NOT EXISTS jobs(
              name TEXT PRIMARY KEY,
              last_run TEXT
            );

            /* 附件：原件落在 files/ 下，这里只存元信息和抽出来的正文。
               origin=upload 有原件；origin=remote 是站点托管、只剩名字和预览图 */
            CREATE TABLE IF NOT EXISTS attachments(
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              card_id INTEGER DEFAULT 0,
              created_at TEXT,
              sha1 TEXT DEFAULT '',
              name TEXT DEFAULT '',
              mime TEXT DEFAULT '',
              kind TEXT DEFAULT 'other',
              size INTEGER DEFAULT 0,
              path TEXT DEFAULT '',
              remote TEXT DEFAULT '',
              text TEXT DEFAULT '',
              text_len INTEGER DEFAULT 0,
              origin TEXT DEFAULT 'upload',
              UNIQUE(card_id, sha1)
            );
            CREATE INDEX IF NOT EXISTS idx_att_card ON attachments(card_id);
            """
        )
        for col, decl in [
            ("answer", "TEXT DEFAULT ''"),
            ("answer_status", "TEXT DEFAULT ''"),
            ("answer_fmt", "TEXT DEFAULT ''"),
            ("gist", "TEXT DEFAULT ''"),
            ("summary_at", "TEXT"),
            ("exported_at", "TEXT"),
            # 线索（一次连续对话）里的位置。thread_id 为空 = 散卡，行为跟以前一样。
            ("thread_id", "TEXT DEFAULT ''"),
            ("turn_no", "INTEGER DEFAULT 0"),
            ("parent_id", "INTEGER"),
            ("relation", "TEXT DEFAULT ''"),
            ("ask_type", "TEXT DEFAULT ''"),
        ]:
            _ensure_col(c, "cards", col, decl)
        c.execute("CREATE INDEX IF NOT EXISTS idx_thread ON cards(thread_id, turn_no)")
        # 早期自动标题是整句原话（"我知道了，……"），统一收一下；手动改过的标题不动
        from classify import tidy_question
        for r in c.execute("SELECT id, title FROM threads WHERE COALESCE(title_auto,1)=1").fetchall():
            t = tidy_question(r["title"] or "", 24)
            if t and t != r["title"]:
                c.execute("UPDATE threads SET title=? WHERE id=?", (t, r["id"]))
        # 老数据没有 fmt，按内容猜一次，免得旧卡片渲染成纯文本
        rows = c.execute("SELECT id, answer FROM cards WHERE length(answer)>0 AND (answer_fmt IS NULL OR answer_fmt='')").fetchall()
        for r in rows:
            c.execute("UPDATE cards SET answer_fmt=? WHERE id=?", (guess_fmt(r[1]), r[0]))


MD_HINT = re.compile(
    r"(^#{1,6}\s|\n```|^\s*```|^\s*[-*+]\s+\S|\n\s*\d+[.)]\s+\S|\*\*[^*\n]+\*\*|\|.*\|)",
    re.M,
)


def guess_fmt(answer):
    """没显式给格式时，按内容猜是 markdown 还是纯文本。"""
    return "md" if MD_HINT.search(answer or "") else "text"


# ---------------------------------------------------------------- 卡片

def add_card(text, source="", url="", weight=None, created_at=None,
             thread_id="", turn_no=None, parent_id=None):
    """新建一张卡。

    带 thread_id 时，这张卡就是那条线索的第 turn_no 轮：父节点默认指向前一轮，
    并自动标记问法和承接关系。同一个问题在不同线索里不会互相顶掉（哈希里带了线索和轮次）。
    """
    text = (text or "").strip()
    if len(text) < 2:
        return None
    thread_id = (thread_id or "").strip()[:120]
    w = weight if weight in CAP_DAYS else None
    from classify import score_text, ask_type as _ask_type, relation_of
    score, tags, auto_w = score_text(text)
    at = _ask_type(text)
    now = created_at or now_str()
    # 新卡当天就进复习队列（等同 Anki 的 new card），否则第一天打开复习页是空的，容易被当成坏了
    due = datetime.now().strftime(FMT)

    if thread_id:
        # 轮次以服务端为准：网页端无论是首次发问、刷新页面还是改名重连，顺序都不会乱
        nxt = next_turn(thread_id)
        try:
            turn_no = max(int(turn_no or 0), nxt)
        except (TypeError, ValueError):
            turn_no = nxt
        h = text_hash("t|%s|%d|%s" % (thread_id, turn_no, text))
    else:
        turn_no, parent_id = 0, None
        h = text_hash(text)

    with db() as c:
        if thread_id and parent_id is None and turn_no > 1:
            prevs = [dict(r) for r in c.execute(
                "SELECT id, text, answer, turn_no FROM cards WHERE thread_id=? AND turn_no<? "
                "ORDER BY turn_no ASC, id ASC", (thread_id, turn_no))]
            parent_id = _pick_parent(prevs, text)
        relation = "" if not thread_id else relation_of(text, bool(parent_id))

        cur = c.execute(
            """INSERT OR IGNORE INTO cards
               (created_at, source, url, text, text_hash, score, weight, tags, next_due,
                thread_id, turn_no, parent_id, relation, ask_type)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (now, source or "", url or "", text, h, score, w or auto_w, tags, due,
             thread_id, turn_no, parent_id, relation, at),
        )
        if cur.rowcount == 0:
            row = c.execute("SELECT * FROM cards WHERE text_hash=?", (h,)).fetchone()
            if thread_id:
                _ensure_thread(c, thread_id, text, source, url, now)
                _refresh_thread(c, thread_id)
            return dict(row) if row else None
        row = c.execute("SELECT * FROM cards WHERE id=?", (cur.lastrowid,)).fetchone()
        if thread_id:
            _ensure_thread(c, thread_id, text, source, url, now)
            _refresh_thread(c, thread_id)
        return dict(row)


def get_card(cid):
    with db() as c:
        row = c.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        card = dict(row)
        _attach_files(c, [card], with_text=True)
        return card


# ---------------------------------------------------------------- 附件

def add_attachment(card_id, meta):
    """把 attach.save_bytes / save_remote 的结果挂到卡片上。同一份文件挂两次只留一条。"""
    if not meta or not card_id:
        return None
    with db() as c:
        c.execute(
            """INSERT OR IGNORE INTO attachments
               (card_id, created_at, sha1, name, mime, kind, size, path, remote, text, text_len, origin)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (int(card_id), now_str(), meta.get("sha1", ""), meta.get("name", ""),
             meta.get("mime", ""), meta.get("kind", "other"), int(meta.get("size") or 0),
             meta.get("path", ""), meta.get("remote", ""), meta.get("text", ""),
             len(meta.get("text") or ""), meta.get("origin", "upload")),
        )
        row = c.execute(
            "SELECT * FROM attachments WHERE card_id=? AND sha1=? ORDER BY id DESC LIMIT 1",
            (int(card_id), meta.get("sha1", "")),
        ).fetchone()
        return dict(row) if row else None


def attachments_for(cids):
    """批量取附件，避免列表页 N+1。返回 {card_id: [att...]}。"""
    ids = [int(x) for x in (cids or []) if x]
    if not ids:
        return {}
    out = {}
    with db() as c:
        for chunk in range(0, len(ids), 400):
            part = ids[chunk:chunk + 400]
            marks = ",".join("?" * len(part))
            for r in c.execute(
                "SELECT id, card_id, name, mime, kind, size, path, remote, origin, text_len, created_at "
                "FROM attachments WHERE card_id IN (%s) ORDER BY id ASC" % marks, tuple(part)
            ):
                out.setdefault(r["card_id"], []).append(dict(r))
    return out


def _attach_files(c, rows, with_text=False):
    """给一批卡片补上 atts 字段（UI 直接用），顺带把第一份附件的一句话摘要写成 file_hint。"""
    if not rows:
        return rows
    by_card = attachments_for([r.get("id") for r in rows])
    for r in rows:
        atts = by_card.get(r.get("id"), [])
        r["atts"] = atts
        if atts:
            r["file_hint"] = atts[0].get("name", "")
    return rows


def get_attachment(aid):
    with db() as c:
        row = c.execute("SELECT * FROM attachments WHERE id=?", (aid,)).fetchone()
        return dict(row) if row else None


def all_attachments():
    """导出用：一次拿全，按卡片分组。"""
    out = {}
    with db() as c:
        for r in c.execute("SELECT * FROM attachments ORDER BY card_id, id"):
            out.setdefault(r["card_id"], []).append(dict(r))
    return out


def delete_attachment(aid):
    with db() as c:
        row = c.execute("SELECT path FROM attachments WHERE id=?", (aid,)).fetchone()
        c.execute("DELETE FROM attachments WHERE id=?", (aid,))
    if row and row["path"]:
        import attach as _at
        p = _at.abs_path(row["path"])
        if p:
            try:
                os.remove(p)
            except Exception:
                pass
    return True



def attach_answer(prompt, answer, status="ok", fmt="", cid=None):
    """把答案挂到卡片上。

    优先用 cid（抓取脚本会带回卡片 id，这样同一句话问两次也不会挂错）；
    拿不到 id 再按 prompt 兜底找卡，找不到就补建一张。

    fmt: 'md' / 'text'；留空则按内容自动判断。"""
    answer = (answer or "").strip()
    if fmt not in ("md", "text"):
        fmt = guess_fmt(answer)
    # status=missing 表示"确认抓取失败"，要记下这个状态，所以允许空答案
    if not answer and status != "missing":
        return None
    if not cid:
        h = text_hash(prompt)
        with db() as c:
            row = c.execute("SELECT id FROM cards WHERE text_hash=?", (h,)).fetchone()
        cid = row["id"] if row else None
    if not cid:
        card = add_card(prompt, source="网页")
        cid = card["id"] if card else None
    if not cid:
        return None
    from classify import score_answer
    with db() as c:
        row = c.execute("SELECT score, weight, manual FROM cards WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        new_score = row["score"] or 0
        new_weight = row["weight"] or "mid"
        if status == "ok":
            new_score = max(new_score, min(100, 24 + score_answer(answer)))
            if not row["manual"]:
                cand = "high" if new_score >= 58 else ("mid" if new_score >= 30 else "low")
                order = {"low": 0, "mid": 1, "high": 2}
                if order.get(cand, 1) > order.get(new_weight, 1):
                    new_weight = cand
        c.execute(
            "UPDATE cards SET answer=?, answer_status=?, answer_fmt=?, score=?, weight=? WHERE id=?",
            (answer, status, fmt, new_score, new_weight, cid),
        )
    return get_card(cid)


def sweep():
    with db() as c:
        c.execute(
            """UPDATE cards SET archived=1 WHERE archived=0 AND (
                 (weight='low' AND julianday('now')-julianday(created_at) > 30) OR
                 (weight='mid' AND julianday('now')-julianday(created_at) > 180)
               )"""
        )


def list_cards(view="due", q="", limit=300):
    where, args = ["1=1"], []
    if view == "due":
        where.append("archived=0 AND next_due<=?")
        args.append(now_str())
        order = "score DESC, next_due ASC"
    elif view == "high":
        where.append("archived=0 AND weight='high'")
        order = "created_at DESC"
    elif view == "archived":
        where.append("archived=1")
        order = "created_at DESC"
    elif view == "answered":
        where.append("archived=0 AND length(answer)>0")
        order = "created_at DESC"
    elif view == "today":
        where.append("archived=0 AND created_at LIKE ?")
        args.append(datetime.now().strftime("%Y-%m-%d") + "%")
        order = "created_at ASC"
    else:
        where.append("archived=0")
        order = "created_at DESC"
    if q:
        where.append("(text LIKE ? OR conclusion LIKE ? OR answer LIKE ? OR tags LIKE ?"
                     " OR EXISTS(SELECT 1 FROM attachments f WHERE f.card_id=cards.id AND (f.text LIKE ? OR f.name LIKE ?)))")
        args += ["%" + q + "%"] * 6
    sql = "SELECT * FROM cards WHERE " + " AND ".join(where) + " ORDER BY " + order + " LIMIT %d" % limit
    with db() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        _attach_thread_title(c, rows)
        return _attach_files(c, rows)


def _attach_thread_title(c, rows):
    """给卡片补上所属线索的标题，列表里才看得出"第几轮、在追什么"。"""
    tids = {r.get("thread_id") for r in rows if r.get("thread_id")}
    if not tids:
        return
    marks = ",".join("?" * len(tids))
    titles = {r["id"]: r["title"] for r in c.execute(
        "SELECT id, title FROM threads WHERE id IN (%s)" % marks, tuple(tids))}
    for r in rows:
        r["thread_title"] = titles.get(r.get("thread_id"), "")


def cards_between(start_date, end_date):
    """start_date/end_date 为 YYYY-MM-DD，含首尾。"""
    with db() as c:
        rows = c.execute(
            "SELECT * FROM cards WHERE date(created_at)>=? AND date(created_at)<=? ORDER BY created_at ASC",
            (start_date, end_date),
        )
        out = [dict(r) for r in rows]
        _attach_thread_title(c, out)
        return out


def live_cards():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM cards WHERE archived=0 ORDER BY created_at DESC LIMIT 3000")]


def review_card(cid, grade):
    with db() as c:
        row = c.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
        if not row:
            return None
        w = row["weight"] if row["weight"] in CAP_DAYS else "mid"
        cap, base = CAP_DAYS[w], BASE_DAYS[w]
        iv = float(row["interval_days"] or 0)
        if grade <= 0:
            iv = base
        elif grade == 1:
            iv = max(base, round(iv * 1.7, 1)) if iv else base
        else:
            iv = max(base, round(iv * 2.5, 1)) if iv else base
        iv = min(iv, cap)
        due = (datetime.now() + timedelta(days=iv)).strftime(FMT)
        c.execute(
            "UPDATE cards SET interval_days=?, next_due=?, review_count=review_count+1, last_review=? WHERE id=?",
            (iv, due, now_str(), cid),
        )
        return {"id": cid, "interval_days": iv, "next_due": due, "cap": cap}


def set_field(cid, **kv):
    allowed = {"weight", "conclusion", "gist", "archived", "tags", "manual"}
    sets, args = [], []
    for k, v in kv.items():
        if k in allowed:
            sets.append("%s=?" % k)
            args.append(v)
    if not sets:
        return False
    args.append(cid)
    with db() as c:
        c.execute("UPDATE cards SET " + ", ".join(sets) + " WHERE id=?", args)
    return True


def delete_card(cid):
    with db() as c:
        row = c.execute("SELECT thread_id FROM cards WHERE id=?", (cid,)).fetchone()
        c.execute("DELETE FROM cards WHERE id=?", (cid,))
        c.execute("DELETE FROM links WHERE a=? OR b=?", (cid, cid))
        c.execute("DELETE FROM attachments WHERE card_id=?", (cid,))
        if row and row["thread_id"]:
            c.execute("UPDATE cards SET parent_id=NULL WHERE parent_id=?", (cid,))
            _refresh_thread(c, row["thread_id"])
    return True


def stats():
    with db() as c:
        g = lambda sql, a=(): c.execute(sql, a).fetchone()[0]
        return {
            "total": g("SELECT COUNT(*) FROM cards WHERE archived=0"),
            "high": g("SELECT COUNT(*) FROM cards WHERE archived=0 AND weight='high'"),
            "mid": g("SELECT COUNT(*) FROM cards WHERE archived=0 AND weight='mid'"),
            "low": g("SELECT COUNT(*) FROM cards WHERE archived=0 AND weight='low'"),
            "due": g("SELECT COUNT(*) FROM cards WHERE archived=0 AND next_due<=?", (now_str(),)),
            "archived": g("SELECT COUNT(*) FROM cards WHERE archived=1"),
            "today": g("SELECT COUNT(*) FROM cards WHERE archived=0 AND created_at LIKE ?", (datetime.now().strftime("%Y-%m-%d") + "%",)),
            "with_answer": g("SELECT COUNT(*) FROM cards WHERE archived=0 AND length(answer)>0"),
            "links": g("SELECT COUNT(*) FROM links"),
            "threads": g("SELECT COUNT(*) FROM threads WHERE COALESCE(archived,0)=0"),
            "threaded": g("SELECT COUNT(*) FROM cards WHERE archived=0 AND COALESCE(thread_id,'')<>''"),
            "loose": g("SELECT COUNT(*) FROM cards WHERE archived=0 AND COALESCE(thread_id,'')=''"),
        }


# ---------------------------------------------------------------- 线索（一次连续对话）

def _pick_parent(prevs, text):
    """判断这一问是在追哪一轮。

    短承接（"举个例子"、"为什么"）挂上一轮；带自己主题的问句，
    拿去和前面每一轮的「问题+回答」比字面重合度，挂到最像的那一轮。
    这样"它和弱监督的区别"会回到主干，而不是在半路上越挂越深。
    """
    if not prevs:
        return None
    from classify import similarity
    t = (text or "").strip()
    prev = prevs[-1]["id"]
    scored = []
    for r in prevs:
        hay = (r.get("text") or "") + " " + ((r.get("answer") or "")[:500])
        scored.append((similarity(t, hay), r["id"]))
    scored.sort(key=lambda x: -x[0])
    if not scored:
        return prev
    top_s, top_id = scored[0]
    # 最像的就是上一轮，或者都不够像 → 老实挂在上一轮下面，别乱跳
    return top_id if (top_id != prev and top_s >= 0.15) else prev


def _ensure_thread(c, tid, title="", source="", url="", when=None):
    """拿到线索行；没有就按第一句话建一条。不提交事务，交给调用方。"""
    now = when or now_str()
    row = c.execute("SELECT * FROM threads WHERE id=?", (tid,)).fetchone()
    if row:
        c.execute(
            """UPDATE threads SET updated_at=?,
                 source=CASE WHEN COALESCE(source,'')='' THEN ? ELSE source END,
                 url=CASE WHEN COALESCE(url,'')='' THEN ? ELSE url END
               WHERE id=?""",
            (now, source or "", url or "", tid),
        )
        return dict(row)
    t = (title or "").strip().split("\n")[0][:40] or "未命名线索"
    from classify import tidy_question
    t = tidy_question(t, 24) or "未命名线索"
    c.execute(
        "INSERT INTO threads(id,title,source,url,created_at,updated_at,turns) VALUES (?,?,?,?,?,?,0)",
        (tid, t, source or "", url or "", now, now),
    )
    return {"id": tid, "title": t, "intent": "", "outline": ""}


def _refresh_thread(c, tid):
    c.execute(
        "UPDATE threads SET turns=(SELECT COUNT(*) FROM cards WHERE thread_id=?) WHERE id=?",
        (tid, tid),
    )


def ensure_thread(tid, title="", source="", url=""):
    tid = (tid or "").strip()[:120]
    if not tid:
        return None
    with db() as c:
        th = _ensure_thread(c, tid, title, source, url)
        _refresh_thread(c, tid)
        return th


def next_turn(tid):
    with db() as c:
        r = c.execute("SELECT MAX(turn_no) FROM cards WHERE thread_id=?", (tid,)).fetchone()
        return int(r[0] or 0) + 1


def get_thread(tid):
    with db() as c:
        row = c.execute("SELECT * FROM threads WHERE id=?", (tid,)).fetchone()
        return dict(row) if row else None


def list_threads(q="", limit=200, include_archived=False):
    """线索列表，附带前几轮问题做预览。"""
    where, args = [], []
    if not include_archived:
        where.append("COALESCE(t.archived,0)=0")
    if q:
        where.append("(t.title LIKE ? OR t.intent LIKE ? OR EXISTS("
                     "SELECT 1 FROM cards c WHERE c.thread_id=t.id AND c.text LIKE ?))")
        args += ["%" + q + "%"] * 3
    sql = ("SELECT t.* FROM threads t "
           + ("WHERE " + " AND ".join(where) + " " if where else "")
           + "ORDER BY t.updated_at DESC LIMIT %d" % limit)
    with db() as c:
        rows = [dict(r) for r in c.execute(sql, args)]
        for t in rows:
            ps = c.execute(
                "SELECT turn_no, text, ask_type FROM cards WHERE thread_id=? ORDER BY turn_no ASC, id ASC LIMIT 6",
                (t["id"],),
            ).fetchall()
            t["preview"] = [(p["text"] or "").replace("\n", " ")[:26] for p in ps]
            t["last_q"] = ps[-1]["text"][:80] if ps else ""
            t["has_answer"] = bool(c.execute(
                "SELECT 1 FROM cards WHERE thread_id=? AND length(answer)>0 LIMIT 1", (t["id"],)
            ).fetchone())
    return rows


def thread_cards(tid):
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM cards WHERE thread_id=? ORDER BY turn_no ASC, id ASC", (tid,)
        )]


def thread_tree(tid):
    """按 parent_id 算出每轮的缩进层级，UI 直接用。"""
    rows = thread_cards(tid)
    by_id = {r["id"]: r for r in rows}

    def depth(r):
        d, cur, guard, seen = 0, r, 0, set()
        while guard < 20:
            pid = cur.get("parent_id")
            if not pid or pid not in by_id or pid in seen:
                break
            seen.add(pid)
            cur = by_id[pid]
            d += 1
            guard += 1
        return min(d, 4)

    for r in rows:
        r["depth"] = depth(r)
    return _attach_files(None, rows)


def loose_cards(limit=80):
    """不属于任何线索的散卡（剪贴板抓的、手记的）。"""
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM cards WHERE COALESCE(thread_id,'')='' AND archived=0 "
            "ORDER BY created_at DESC LIMIT ?", (limit,)
        )]
        return _attach_files(c, rows)


def set_thread_fields(tid, **kv):
    allowed = {"title", "intent", "outline", "outline_at", "archived", "title_auto", "source", "url"}
    sets, args = [], []
    for k, v in kv.items():
        if k in allowed:
            sets.append("%s=?" % k)
            args.append(v)
    if not sets:
        return False
    args.append(tid)
    with db() as c:
        c.execute("UPDATE threads SET " + ", ".join(sets) + " WHERE id=?", args)
    return True


def rebind_thread(old, new):
    """网页里拿到真正的会话 id 后，把临时的 tab 线索改名/并入。"""
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new or old == new:
        return False
    with db() as c:
        cards = c.execute(
            "SELECT id, turn_no FROM cards WHERE thread_id=? ORDER BY turn_no ASC, id ASC", (old,)
        ).fetchall()
        if not cards:
            c.execute("DELETE FROM threads WHERE id=?", (old,))
            return False
        exists = c.execute("SELECT 1 FROM threads WHERE id=?", (new,)).fetchone()
        base = 0
        if exists:
            base = c.execute("SELECT COALESCE(MAX(turn_no),0) FROM cards WHERE thread_id=?", (new,)).fetchone()[0] or 0
        for i, r in enumerate(cards, 1):
            c.execute("UPDATE cards SET thread_id=?, turn_no=? WHERE id=?",
                      (new, base + (r["turn_no"] or i), r["id"]))
        if exists:
            c.execute("DELETE FROM threads WHERE id=?", (old,))
        else:
            c.execute("UPDATE threads SET id=?, updated_at=? WHERE id=?", (new, now_str(), old))
        _fix_parents(c, new)
        _refresh_thread(c, new)
    return True


def _fix_parents(c, tid):
    """父节点必须落在同一条线索里，否则清空（拆分/合并后会出现跨线引用）。"""
    rows = c.execute("SELECT id, parent_id FROM cards WHERE thread_id=?", (tid,)).fetchall()
    ids = {r["id"] for r in rows}
    for r in rows:
        if r["parent_id"] and r["parent_id"] not in ids:
            c.execute("UPDATE cards SET parent_id=NULL, relation='起点' WHERE id=?", (r["id"],))


def split_thread(tid, from_turn):
    """从第 N 轮起另立一条线索：话题跑偏时用，原来的线保持干净。"""
    new_id = None
    with db() as c:
        rows = c.execute(
            "SELECT * FROM cards WHERE thread_id=? ORDER BY turn_no ASC, id ASC", (tid,)
        ).fetchall()
        if not rows:
            return None
        from_turn = int(from_turn)
        moved = [r for r in rows if (r["turn_no"] or 0) >= from_turn]
        if not moved:
            return None
        new_id = "%s#%d" % (tid, from_turn)
        suffix = 2
        while c.execute("SELECT 1 FROM threads WHERE id=?", (new_id,)).fetchone():
            new_id = "%s#%d-%d" % (tid, from_turn, suffix)
            suffix += 1
        first = moved[0]
        now = now_str()
        c.execute(
            "INSERT INTO threads(id,title,source,url,created_at,updated_at,turns) VALUES (?,?,?,?,?,?,0)",
            (new_id, (first["text"] or "").strip().split("\n")[0][:40] or "未命名线索",
             first["source"] or "", first["url"] or "", now, now),
        )
        for i, r in enumerate(moved, 1):
            c.execute("UPDATE cards SET thread_id=?, turn_no=? WHERE id=?", (new_id, i, r["id"]))
        c.execute("UPDATE cards SET parent_id=NULL, relation='起点' WHERE id=?", (first["id"],))
        _fix_parents(c, new_id)
        _refresh_thread(c, new_id)
        _refresh_thread(c, tid)
    # 事务提交后再读，否则新连接读不到刚写的行
    return get_thread(new_id)


def set_parent(cid, parent_id):
    """手动改挂靠：把某一轮挂到另一轮下面（结构不对时自己掰正）。"""
    from classify import relation_of
    if parent_id == cid:
        return False
    with db() as c:
        row = c.execute("SELECT thread_id, text FROM cards WHERE id=?", (cid,)).fetchone()
        if not row:
            return False
        c.execute("UPDATE cards SET parent_id=?, relation=? WHERE id=?",
                  (parent_id, relation_of(row["text"], bool(parent_id)), cid))
    return True


def move_card(cid, tid, parent_id=None):
    """手动把一张卡挪进另一条线索（补结构用）。"""
    from classify import relation_of
    with db() as c:
        row = c.execute("SELECT * FROM cards WHERE id=?", (cid,)).fetchone()
        if not row:
            return False
        n = (c.execute("SELECT COALESCE(MAX(turn_no),0) FROM cards WHERE thread_id=?", (tid,)).fetchone()[0] or 0) + 1
        c.execute("UPDATE cards SET thread_id=?, turn_no=?, parent_id=?, relation=? WHERE id=?",
                  (tid, n, parent_id, relation_of(row["text"], bool(parent_id)), cid))
        _refresh_thread(c, tid)
    return True


# ---------------------------------------------------------------- 把散卡补成线索
# 网页会话的 URL 里本来就带着会话 id（豆包 /chat/xxx、DeepSeek /a/chat/s/xxx…），
# 以前只存不用。同一 URL 的散卡，其实就是同一次对话，可以直接归回一条线索。

_URL_CONV = re.compile(r"/(?:chat|c|a/chat/s|qianwen)/([A-Za-z0-9_-]{6,})")


def thread_id_from_url(url):
    m = _URL_CONV.search(url or "")
    return ("url:" + m.group(1)) if m else ""


def _link_into_thread(c, tid, rows, title=""):
    """按时间顺序把一批卡排成一条线索，父节点用语义判断。不改 text_hash。"""
    from classify import ask_type, relation_of
    tid = (tid or "").strip()[:120]
    if not rows:
        return None
    _ensure_thread(c, tid, title or rows[0].get("text") or "",
                   rows[0].get("source") or "", rows[0].get("url") or "")
    prevs = []
    for i, r in enumerate(rows, 1):
        pid = _pick_parent(prevs, r.get("text")) if prevs else None
        c.execute(
            "UPDATE cards SET thread_id=?, turn_no=?, parent_id=?, relation=?, ask_type=? WHERE id=?",
            (tid, i, pid, relation_of(r.get("text") or "", bool(pid)),
             ask_type(r.get("text") or ""), r["id"]),
        )
        prevs.append(r)
    _fix_parents(c, tid)
    _refresh_thread(c, tid)
    return tid


def backfill_threads():
    """启动时跑一次：同一个网页会话里的散卡，自动归回一条线索。返回新建条数。"""
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM cards WHERE COALESCE(thread_id,'')='' AND COALESCE(url,'')<>'' "
            "ORDER BY created_at ASC, id ASC")]
    groups = {}
    for r in rows:
        tid = thread_id_from_url(r.get("url"))
        if tid:
            groups.setdefault(tid, []).append(r)
    made = 0
    for tid, items in groups.items():
        if len(items) < 2:
            continue
        with db() as c:
            if c.execute("SELECT 1 FROM cards WHERE thread_id=? LIMIT 1", (tid,)).fetchone():
                continue          # 已经有线了，别重复归并
            if _link_into_thread(c, tid, items):
                made += 1
    return made


def make_thread(cids, title=""):
    """手动把勾选的几张卡串成一条线索（散卡没有 URL 时用）。"""
    ids = []
    for x in cids or []:
        try:
            ids.append(int(x))
        except (TypeError, ValueError):
            pass
    ids = list(dict.fromkeys(ids))
    if len(ids) < 2:
        return None
    with db() as c:
        rows = [dict(r) for r in c.execute(
            "SELECT * FROM cards WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)]
    rows.sort(key=lambda r: ((r.get("created_at") or ""), r["id"]))
    tid = "manual:%d-%d" % (rows[0]["id"], len(rows))
    if get_thread(tid):
        return get_thread(tid)
    with db() as c:
        _link_into_thread(c, tid, rows, title)
    return get_thread(tid)


# ---------------------------------------------------------------- 关联链

def add_link(a, b, relation="同主题", reason="", origin="local"):
    if a == b:
        return False
    x, y = (a, b) if a < b else (b, a)
    with db() as c:
        cur = c.execute(
            """INSERT INTO links(a,b,relation,reason,origin,created_at) VALUES (?,?,?,?,?,?)
               ON CONFLICT(a,b) DO UPDATE SET
                 relation=excluded.relation, reason=excluded.reason, origin=excluded.origin
               WHERE excluded.origin='llm'""",
            (x, y, relation, reason, origin, now_str()),
        )
        return cur.rowcount > 0


def links_of(cid):
    with db() as c:
        rows = c.execute(
            """SELECT l.*, CASE WHEN l.a=? THEN l.b ELSE l.a END AS other
               FROM links l WHERE l.a=? OR l.b=?""", (cid, cid, cid)
        ).fetchall()
        out = []
        for r in rows:
            o = c.execute("SELECT id,text,weight,tags FROM cards WHERE id=?", (r["other"],)).fetchone()
            if o:
                out.append({"id": o["id"], "text": o["text"], "weight": o["weight"],
                            "tags": o["tags"], "relation": r["relation"], "reason": r["reason"]})
        return out


def all_links():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM links ORDER BY id DESC")]


# ---------------------------------------------------------------- 日报周报

def save_digest(kind, period, content):
    with db() as c:
        c.execute(
            "INSERT INTO digests(kind,period,created_at,content) VALUES (?,?,?,?) "
            "ON CONFLICT(kind,period) DO UPDATE SET content=excluded.content, created_at=excluded.created_at",
            (kind, period, now_str(), content),
        )
    return True


def get_digest(kind, period):
    with db() as c:
        row = c.execute("SELECT * FROM digests WHERE kind=? AND period=?", (kind, period)).fetchone()
        return dict(row) if row else None


def list_digests(kind=None, limit=30):
    with db() as c:
        if kind:
            rows = c.execute("SELECT * FROM digests WHERE kind=? ORDER BY period DESC LIMIT ?", (kind, limit))
        else:
            rows = c.execute("SELECT * FROM digests ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- 任务状态

def job_last(name):
    with db() as c:
        row = c.execute("SELECT last_run FROM jobs WHERE name=?", (name,)).fetchone()
        return row["last_run"] if row else None


def job_set(name, when=None):
    with db() as c:
        c.execute(
            "INSERT INTO jobs(name,last_run) VALUES (?,?) ON CONFLICT(name) DO UPDATE SET last_run=excluded.last_run",
            (name, when or now_str()),
        )


# ---------------------------------------------------------------- 命令行

def _cli():
    """给 WorkBuddy 会话用：python store.py add "问题" [回答|-] [来源]
    回答传 - 表示从 stdin 读（长文本、带代码块时用这个，避开 shell 转义）。"""
    import sys
    init_db()
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print('用法：')
        print('  python store.py add "问题" "回答" [来源]')
        print('  python store.py add "问题" - WorkBuddy   ← 回答从 stdin 读')
        print('  python store.py list [条数]')
        return

    cmd = args[0]
    if cmd == "add":
        text = args[1] if len(args) > 1 else ""
        if not text:
            print("FAILED: 问题为空")
            return
        ans = args[2] if len(args) > 2 else ""
        if ans == "-":
            ans = sys.stdin.read()
        src = args[3] if len(args) > 3 and not args[3].startswith("--") else "WorkBuddy"
        force = None
        for flag in ("--high", "--mid", "--low"):
            if flag in sys.argv:
                force = flag[2:]
        card = add_card(text, src, weight=force)
        if not card:
            print("FAILED: 无法写入")
            return
        if ans.strip():
            attach_answer(text, ans.strip())
        if force:
            set_field(card["id"], weight=force, manual=1)
        c = get_card(card["id"])
        print("OK #%d [%s] score=%d tags=%s | 答案 %d 字"
              % (c["id"], c["weight"], c["score"], c["tags"] or "-", len(c["answer"] or "")))
        return

    if cmd == "list":
        n = int(args[1]) if len(args) > 1 else 5
        for c in list_cards("all", limit=n):
            print("#%-3d [%s] %s%s" % (c["id"], c["weight"],
                                       c["text"][:46].replace("\n", " "),
                                       "" if c["answer"] else "  ← 无答案"))
        return

    print("unknown command:", cmd)


if __name__ == "__main__":
    _cli()
