#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""导出为 Obsidian 库：每张卡一个 md，带 YAML frontmatter 和 [[双链]]，
日报/周报作为索引页。所有链接指向真实文件名，graph view 里能直接连起来。
"""

import hashlib
import json
import os
import re
import shutil

import attach as attach_mod
import store

ILLEGAL = re.compile(r'[\\/:*?"<>|#^\[\]\r\n\t]')


def slug(text, limit=20):
    s = ILLEGAL.sub(" ", (text or "").strip())
    s = re.sub(r"\s+", " ", s).strip()
    s = s.replace(" ", "-")
    return (s[:limit] or "无标题").rstrip("-")


def stem_of(card):
    """文件名（不含 .md），也是双链目标。稳定可复现。"""
    day = (card.get("created_at") or "0000-00-00")[:10].replace("-", "")
    return "%s-%03d %s" % (day, int(card["id"]), slug(card.get("text")))


def _frontmatter(d):
    lines = ["---"]
    for k, v in d.items():
        if isinstance(v, list):
            lines.append("%s:" % k)
            for i in v:
                lines.append("  - %s" % i)
        else:
            lines.append("%s: %s" % (k, v))
    lines.append("---")
    return "\n".join(lines)


def _sig(*parts):
    """内容指纹：写进 frontmatter 的 sync 字段，用来判断这篇要不要重写。"""
    h = hashlib.sha1()
    for p in parts:
        h.update(("%s" % (p or "")).encode("utf-8", "ignore"))
        h.update(b"\x1f")
    return h.hexdigest()[:12]


def _sync_of(path):
    """读已有笔记头部的指纹；没有（老文件 / 用户在 Obsidian 里改过）返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            head = f.read(900)
    except Exception:
        return None
    m = re.search(r"^sync:\s*(\S+)", head, re.M)
    return m.group(1) if m else None


def _card_md(card, links, link, atts=None, copied=None):
    body = []
    first_line = (card["text"] or "").split("\n")[0][:60]
    body.append("# %s" % first_line)
    body.append("")
    body.append("> [!question] 原问题")
    for line in (card["text"] or "").split("\n"):
        body.append("> " + line)
    body.append("")

    if atts:
        body.append("## 附件")
        body.append("")
        body.extend(_att_lines(atts, copied or {}))
        body.append("")

    concl = (card.get("conclusion") or "").strip()
    gist = (card.get("gist") or "").strip()
    if concl or gist:
        body.append("## 我的结论")
        body.append("")
        body.append(concl or gist)
        body.append("")
    else:
        body.append("## 我的结论")
        body.append("")
        body.append("_（待补：这一句是这张卡的价值所在）_")
        body.append("")

    ans = (card.get("answer") or "").strip()
    if ans:
        body.append("## AI 回答")
        body.append("")
        body.append(ans)
        body.append("")

    rel = []
    for l in links:
        tgt = link(l["id"])
        if tgt and tgt not in [x[0] for x in rel]:
            rel.append((tgt, l.get("relation") or "同主题", l.get("reason") or ""))
    if rel:
        body.append("## 关联")
        body.append("")
        for tgt, relation, reason in rel[:8]:
            body.append("- %s · %s%s" % (tgt, relation, ("　—— " + reason) if reason else ""))
        body.append("")

    if card.get("url"):
        body.append("来源：%s" % card["url"])
        body.append("")

    body.append("---")
    body.append("复习：第 %d 次 ｜ 间隔 %s 天 ｜ 下次 %s" % (
        card.get("review_count") or 0, card.get("interval_days") or 0,
        (card.get("next_due") or "-")[:10]))
    return "\n".join(body)


def _daily_md(day, digest, link):
    out = [_frontmatter({"tags": ["ai-daily"], "date": day, "headline": digest.get("headline", "")}), ""]
    out.append("# %s 日报" % day)
    out.append("")
    if digest.get("headline"):
        out.append("> %s" % digest["headline"])
        out.append("")
    if digest.get("themes"):
        out.append("## 主题")
        out.append("")
        for t in digest["themes"]:
            out.append("### %s" % t.get("name", ""))
            out.append("")
            if t.get("insight"):
                out.append(t["insight"])
                out.append("")
            seen = set()
            for cid in t.get("cards") or []:
                tgt = link(cid)
                if tgt and tgt not in seen:
                    seen.add(tgt)
                    out.append("- %s" % tgt)
            out.append("")
    items = digest.get("cards") or []
    if items:
        rows, order = {}, []
        for it in items:
            tgt = link(it.get("id"))
            if not tgt:
                continue
            g = (it.get("gist") or "").replace("|", "/").strip()
            if tgt not in rows:
                rows[tgt] = []
                order.append(tgt)
            if g and g not in rows[tgt]:
                rows[tgt].append(g)
        if order:
            out.append("## 卡片清单")
            out.append("")
            out.append("| 卡片 | 结论 |")
            out.append("| --- | --- |")
            for tgt in order:
                out.append("| %s | %s |" % (tgt, "；".join(rows[tgt])))
            out.append("")
    if digest.get("sop"):
        out.append("## 可以固化的")
        out.append("")
        for s in digest["sop"]:
            out.append("- **%s** → %s" % (s.get("pattern", ""), s.get("suggest", "")))
        out.append("")
    return "\n".join(out)


def _weekly_md(period, digest, link):
    rng = digest.get("range") or ["", ""]
    out = [_frontmatter({"tags": ["ai-weekly"], "period": period, "range": "%s ~ %s" % tuple(rng)}), ""]
    out.append("# %s 周报（%s ~ %s）" % (period, rng[0], rng[1]))
    out.append("")
    if digest.get("headline"):
        out.append("> %s" % digest["headline"])
        out.append("")
    if digest.get("threads"):
        out.append("## 主线")
        out.append("")
        for t in digest["threads"]:
            out.append("### %s" % t.get("name", ""))
            out.append("")
            if t.get("progress"):
                out.append(t["progress"])
                out.append("")
            seen = set()
            for cid in t.get("cards") or []:
                tgt = link(cid)
                if tgt and tgt not in seen:
                    seen.add(tgt)
                    out.append("- %s" % tgt)
            out.append("")
    if digest.get("patterns"):
        out.append("## 反复在问的事")
        out.append("")
        for p in digest["patterns"]:
            out.append("- **%s**（%s 次）— %s" % (p.get("name", ""), p.get("count", "?"), p.get("advice", "")))
        out.append("")
    if digest.get("sop"):
        out.append("## 该固化的模板")
        out.append("")
        for s in digest["sop"]:
            if s.get("ready"):
                out.append("- [ ] **%s** → %s" % (s.get("pattern", ""), s.get("suggest", "")))
            else:
                out.append("- **%s** → %s" % (s.get("pattern", ""), s.get("suggest", "")))
        out.append("")
    if digest.get("next"):
        out.append("## 下周")
        out.append("")
        for n in digest["next"]:
            out.append("- [ ] %s" % n)
        out.append("")
    return "\n".join(out)


def _quote_block(text, mark="> "):
    """把一段 markdown 塞进 callout / 引用里，每行都要带前缀，否则会断开。"""
    return "\n".join((mark + ln) if ln.strip() else mark.rstrip()
                     for ln in (text or "").split("\n"))


def _copy_atts(atts, atts_dir):
    """把原件复制进 vault，返回 {attachment_id: 文件名}。

    文件名带 sha1 前缀：同名不同内容的文件不会互相覆盖，Obsidian 的双链也不会串。
    """
    copied = {}
    for a in atts or []:
        if a.get("origin") != "upload" or not a.get("path"):
            continue
        src = attach_mod.abs_path(a["path"])
        if not src:
            continue
        fn = "%s-%s" % ((a.get("sha1") or "")[:8], attach_mod.safe_name(a.get("name") or "附件"))
        dst = os.path.join(atts_dir, fn)
        if not os.path.exists(dst):
            os.makedirs(atts_dir, exist_ok=True)
            try:
                shutil.copy2(src, dst)
            except Exception:
                continue
        copied[a["id"]] = fn
    return copied


def _att_lines(atts, copied):
    """附件的 Obsidian 写法：图片直接嵌入，文档双链 + 原文摘录，拿不到原件的如实标注。"""
    out = []
    for a in atts or []:
        fn = copied.get(a.get("id"))
        name = a.get("name") or fn or "附件"
        if fn:
            if a.get("kind") == "image":
                out.append("- ![[%s]]" % fn)
            else:
                out.append("- [[%s|%s]]" % (fn, name))
                text = (a.get("text") or "").strip()
                if text:
                    out.append("")
                    out.append("  > [!quote]- 原文摘录")
                    for ln in text[:1200].split("\n")[:18]:
                        out.append("  > " + ln if ln.strip() else "  >")
                    out.append("")
        elif a.get("remote"):
            out.append("- [%s](%s) · 站点托管，本地只存了链接" % (name, a["remote"]))
        else:
            out.append("- %s · 只记到文件名，原件没取到" % name)
    return out


def _thread_stem(th):
    day = (th.get("created_at") or "0000-00-00")[:10].replace("-", "")
    return "%s %s" % (day, slug(th.get("title") or "线索", 30))


def _thread_md(th, data, turns, link, atts_map=None, copied=None, sync=""):
    """一条线索 = 一篇笔记：先给结构（脉络），再给逐轮对话。"""
    title = th.get("title") or "未命名线索"
    self_stem = _thread_stem(th)
    fm = {
        "type": "topic",
        "title": title,
        "source": th.get("source") or "",
        "created": (th.get("created_at") or "")[:16],
        "turns": len(turns),
        "tags": ["ai-thread", "线索"],
    }
    if sync:
        fm["sync"] = sync
    out = [_frontmatter(fm), ""]
    out.append("# %s" % title)
    out.append("")
    if th.get("intent"):
        out.append("> [!abstract] 这条线索要解决的问题")
        out.append("> %s" % th["intent"])
        out.append("")
    if th.get("url"):
        out.append("来源会话：%s" % th["url"])
        out.append("")

    ol = (data.get("outline") or []) if data else []
    if ol:
        out.append("## 脉络")
        out.append("")
        for it in ol:
            lv = max(1, min(int(it.get("level") or 1), 4))
            # 从 ### 起，让「## 脉络」始终是大纲的父级
            out.append("%s %s" % ("#" * (lv + 2), it.get("heading") or ""))
            if it.get("point"):
                out.append("")
                out.append(it["point"])
            if it.get("turns"):
                out.append("")
                out.append("_（第 %s 轮）_" % "、".join(str(x) for x in it["turns"]))
            out.append("")
    if (data or {}).get("open"):
        out.append("## 还没弄清")
        out.append("")
        for x in data["open"]:
            out.append("- %s" % x)
        out.append("")
    if (data or {}).get("next"):
        out.append("## 下一步")
        out.append("")
        out.append("- [ ] %s" % data["next"])
        out.append("")

    out.append("## 逐轮对话")
    out.append("")
    for c in turns:
        q = (c.get("text") or "").strip()
        head = "### 第%d轮 · %s" % (c.get("turn_no") or 0, (q.split("\n")[0][:50] or "无标题"))
        out.append(head)
        out.append("")
        marks = []
        if c.get("ask_type"):
            marks.append("问法：" + c["ask_type"])
        if c.get("relation"):
            marks.append("关系：" + c["relation"])
        if marks:
            out.append("`%s`" % " ｜ ".join(marks))
            out.append("")
        for line in q.split("\n"):
            out.append("> " + line if line.strip() else ">")
        out.append("")
        al = _att_lines((atts_map or {}).get(int(c["id"])), copied or {})
        if al:
            out.append("**附件**")
            out.append("")
            out.extend(al)
            out.append("")
        concl = (c.get("conclusion") or c.get("gist") or "").strip()
        if concl:
            out.append("**结论**：%s" % concl)
            out.append("")
        ans = (c.get("answer") or "").strip()
        if ans:
            out.append("> [!note]- AI 回答（%d 字）" % len(ans))
            out.append(_quote_block(ans))
            out.append("")

    others = []
    own = {x["id"] for x in turns}
    for c in turns:
        for l in store.links_of(int(c["id"])):
            if l["id"] in own:
                continue
            tgt = link(l["id"])
            if tgt and tgt != "[[%s]]" % self_stem:
                others.append((tgt, l.get("relation") or "同主题"))
    if others:
        out.append("## 关联")
        out.append("")
        seen = set()
        for tgt, rel in others[:8]:
            if tgt in seen:
                continue
            seen.add(tgt)
            out.append("- %s · %s" % (tgt, rel))
        out.append("")

    out.append("---")
    out.append("线索 id：`%s` ｜ 共 %d 轮" % (th.get("id"), len(turns)))
    return "\n".join(out)


def _write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


def _prune_same_thread(dirpath, tid, keep_name):
    """线索改标题会生成新文件名，旧的那篇要收掉，否则库里同一条线有两篇。

    只删自己生成、且正文里写着同一个线索 id 的笔记，别的一律不碰。
    """
    removed = 0
    try:
        names = os.listdir(dirpath)
    except Exception:
        return 0
    for fn in names:
        if not fn.endswith(".md") or fn == keep_name:
            continue
        p = os.path.join(dirpath, fn)
        try:
            if os.path.getsize(p) > 400000:
                continue
            with open(p, "r", encoding="utf-8") as f:
                head = f.read(60000)
        except Exception:
            continue
        if ("线索 id：`%s`" % tid) in head:
            try:
                os.remove(p)
                removed += 1
            except Exception:
                pass
    return removed


def export_all(cfg=None, force=False):
    """force=True 会重写所有笔记（你在 Obsidian 里手改过的内容会被覆盖）。

    不传 force 时按内容指纹判断：内容没变就跳过，变了才重写。
    以前是"导出过一次就永远跳过"，所以后来抓到的答案根本进不去。
    """
    if cfg is None:
        import llm
        cfg = llm.load_config()
    vault = ((cfg.get("obsidian") or {}).get("vault") or "").strip()
    if not vault:
        return {"skipped": "obsidian.vault 未配置"}
    if not os.path.isdir(vault):
        return {"skipped": "vault 目录不存在: %s" % vault}

    sub = (cfg.get("obsidian") or {}).get("subdir") or "AI-Cards"
    thread_as_note = bool((cfg.get("obsidian") or {}).get("thread_as_note", True))
    root = os.path.join(vault, sub)
    cards_dir = os.path.join(root, "cards")
    daily_dir = os.path.join(root, "daily")
    weekly_dir = os.path.join(root, "weekly")
    thread_dir = os.path.join(root, "threads")
    atts_dir = os.path.join(root, "files")

    with store.db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM cards ORDER BY id")]
    stems = {int(r["id"]): stem_of(r) for r in rows}
    atts_map = store.all_attachments()
    copied = _copy_atts([a for v in atts_map.values() for a in v], atts_dir)

    # 先把线索读一遍：既要写线索笔记，也要把"卡片链接"改指到线索笔记，否则日报里全是死链
    thread_list = []
    threaded_ids = set()
    card_thread = {}
    if thread_as_note:
        for th in store.list_threads(limit=2000, include_archived=True):
            turns = store.thread_cards(th["id"])
            if not turns:
                continue
            thread_list.append((th, turns))
            threaded_ids.update(int(c["id"]) for c in turns)
            for c in turns:
                card_thread[int(c["id"])] = _thread_stem(th)

    def link(cid):
        """并入线索的卡片指向那条线索，散卡指向它自己的笔记；都没有就不给链接。"""
        try:
            cid = int(cid)
        except Exception:
            return ""
        if cid in card_thread:
            return "[[%s]]" % card_thread[cid]
        if cid in stems:
            return "[[%s]]" % stems[cid]
        return ""

    # 线索笔记先写：一条线索一篇，结构在笔记里，比一堆散卡好读
    threads_written = 0
    pruned = 0
    if thread_as_note:
        try:
            import llm as _llm
        except Exception:
            _llm = None
        for th, turns in thread_list:
            data = {}
            if th.get("outline"):
                try:
                    data = json.loads(th["outline"])
                except Exception:
                    data = {}
            if not data and _llm:
                data = _llm._local_outline(th, turns)

            sig = _sig(th.get("title"), th.get("intent"), th.get("url"), th.get("outline"),
                       [(c["id"], len(c.get("answer") or ""), c.get("conclusion"),
                         [(a.get("id"), a.get("name")) for a in (atts_map.get(int(c["id"])) or [])])
                        for c in turns])
            stem = _thread_stem(th)
            path = os.path.join(thread_dir, stem + ".md")
            if not force and os.path.exists(path) and _sync_of(path) == sig:
                pruned += _prune_same_thread(thread_dir, th["id"], stem + ".md")
                continue
            _write(path, _thread_md(th, data, turns, link, atts_map, copied, sync=sig) + "\n")
            threads_written += 1
            pruned += _prune_same_thread(thread_dir, th["id"], stem + ".md")

    written = skipped = 0
    for r in rows:
        cid = int(r["id"])
        # 已经并进线索笔记的卡片不再单独成篇，否则 Obsidian 里全是断头卡片
        if thread_as_note and cid in threaded_ids:
            skipped += 1
            continue
        links = store.links_of(cid)
        path = os.path.join(cards_dir, stems[cid] + ".md")
        sig = _sig(r.get("text"), r.get("answer"), r.get("conclusion"), r.get("gist"),
                   r.get("weight"), r.get("tags"),
                   [(a.get("id"), a.get("name"), a.get("text_len")) for a in (atts_map.get(cid) or [])])
        if not force and os.path.exists(path) and _sync_of(path) == sig:
            skipped += 1
            continue
        fm = _frontmatter({
            "title": (r["text"] or "")[:40].split("\n")[0],
            "created": r["created_at"],
            "source": r.get("source") or "",
            "weight": r.get("weight") or "",
            "score": r.get("score") or 0,
            "tags": ["ai-card", {"high": "高权值", "mid": "中权值", "low": "低权值"}.get(r.get("weight"), "")]
                     + [t for t in (r.get("tags") or "").split(",") if t],
            "answer": r.get("answer_status") or ("ok" if r.get("answer") else "none"),
            "sync": sig,
        })
        _write(path, fm + "\n\n" + _card_md(r, links, link, atts_map.get(cid), copied) + "\n")
        with store.db() as c:
            c.execute("UPDATE cards SET exported_at=? WHERE id=?", (store.now_str(), cid))
        written += 1

    for dg in store.list_digests("daily", 90):
        try:
            j = json.loads(dg["content"])
        except Exception:
            continue
        _write(os.path.join(daily_dir, dg["period"] + ".md"), _daily_md(dg["period"], j, link) + "\n")

    for dg in store.list_digests("weekly", 60):
        try:
            j = json.loads(dg["content"])
        except Exception:
            continue
        _write(os.path.join(weekly_dir, dg["period"] + ".md"), _weekly_md(dg["period"], j, link) + "\n")

    return {"cards_written": written, "cards_skipped": skipped, "root": root,
            "threads_written": threads_written, "files_copied": len(copied),
            "pruned": pruned, "total": len(rows)}


if __name__ == "__main__":
    import sys
    store.init_db()
    print(json.dumps(export_all(force="--force" in sys.argv), ensure_ascii=False, indent=2))
