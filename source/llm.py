#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大模型调用与日报/周报生成。

只依赖标准库（urllib），因此任何 OpenAI 兼容接口都能直接接：
DeepSeek / 通义 / 智谱 / Moonshot(Kimi) / 火山方舟 / OpenAI / 本地 Ollama。
没配 api_key 时自动降级为「本地规则汇总」，功能不中断。
"""

import json
import os
import re
import urllib.request
from datetime import date as _date, datetime, timedelta

import paths
import store
from classify import similarity

BASE_DIR = paths.DATA_DIR
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEFAULTS = {
    "api_base": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "enabled": True,
    "timeout": 120,
    "obsidian": {"vault": "", "subdir": "AI-Cards", "thread_as_note": True},
    "schedule": {"daily": "23:30", "weekly": "SUN 22:00"},
    "capture_answer": True,
}


# ---------------------------------------------------------------- 配置

def load_config():
    cfg = json.loads(json.dumps(DEFAULTS))
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                user = json.load(f)
            for k, v in user.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print("[config] parse error:", e)
    return cfg


def save_config(cfg):
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def active(cfg=None):
    cfg = cfg or load_config()
    return bool(cfg.get("enabled") and (cfg.get("api_key") or "").strip() and cfg.get("api_base"))


# ---------------------------------------------------------------- 调用

def chat(messages, cfg=None, temperature=0.3):
    cfg = cfg or load_config()
    base = (cfg.get("api_base") or "").rstrip("/")
    if not base:
        raise RuntimeError("api_base 未配置")
    if not (cfg.get("api_key") or "").strip():
        raise RuntimeError("api_key 未配置")
    body = {"model": cfg.get("model"), "messages": messages, "temperature": temperature}
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + cfg["api_key"].strip(),
        },
    )
    with urllib.request.urlopen(req, timeout=cfg.get("timeout", 120)) as r:
        payload = json.loads(r.read().decode("utf-8"))
    return payload["choices"][0]["message"]["content"]


def parse_json(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j < 0:
        raise ValueError("模型没有返回 JSON")
    return json.loads(t[i:j + 1])


# ---------------------------------------------------------------- 本地关联召回

def recall(card, pool, k=3, thresh=0.10):
    """纯本地候选召回：字符相似度 + 标签重叠 + 共享英文术语。不做语义判断，
    判断留给大模型（或写进 reason 让用户自己看）。"""
    ta = {t for t in (card.get("tags") or "").split(",") if t}
    ea = {w for w in re.findall(r"[a-zA-Z]{4,}", (card.get("text") or "").lower())}
    scored = []
    for other in pool:
        if other["id"] == card["id"]:
            continue
        s = similarity(card["text"], other["text"])
        tb = {t for t in (other.get("tags") or "").split(",") if t}
        if ta & tb:
            s += 0.10
        eb = {w for w in re.findall(r"[a-zA-Z]{4,}", (other.get("text") or "").lower())}
        if ea & eb:
            s += 0.12
        if s >= thresh:
            scored.append((round(s, 3), other))
    scored.sort(key=lambda x: -x[0])
    return scored[:k]


def build_links(cards, pool=None, thresh=0.22):
    """纯本地相似度建链，不需要大模型。返回新建条数。
    阈值刻意偏高：宁可少连，也不要给知识图谱塞噪音。"""
    pool = pool or store.live_cards()
    n = 0
    for c in cards:
        for sim, other in recall(c, pool, thresh=thresh):
            if store.add_link(c["id"], other["id"], "同主题", "文本相似度 %.2f" % sim, "local"):
                n += 1
    return n


# ---------------------------------------------------------------- 线索脉络
# 一串"问题→回答→追问→回答"本身不是知识，重新按逻辑层次排一遍才是。

THREAD_SYS = "你是严谨的知识结构整理助手。只根据给定对话归纳，不补充对话里没有的结论。只输出 JSON，不要解释文字。"

THREAD_TMPL = """下面是我和 AI 的一段连续对话（一条线索），共 {n} 轮。请把它整理成有层次的知识结构。

{blocks}

严格按这个 JSON 输出：
{{
  "title": "这条线索的主题，不超过14字",
  "intent": "我一开始想解决的问题，一句话",
  "outline": [{{"level": 1, "heading": "一级要点", "point": "这一层的结论，30字内", "turns": [轮次]}},
              {{"level": 2, "heading": "二级要点", "point": "", "turns": []}}],
  "open": ["对话里还没解决的疑点"],
  "next": "下一步最值得追问的一个问题"
}}

要求：
1. outline 按逻辑层次重组，不要照抄提问顺序；最多 3 层，总数不超过 10 条；
2. point 必须是对话里真实出现过的结论，没展开就写「未展开」；
3. turns 标注这个要点来自第几轮，可以留空数组；
4. open 最多 3 条，next 一句话；没有就给空数组和空字符串。"""


def _turn_block(turns, ans_limit=700):
    out = []
    for c in turns:
        ans = (c.get("answer") or "").strip()
        if len(ans) > ans_limit:
            ans = ans[:ans_limit] + "…"
        out.append("### 第%d轮（问法：%s，关系：%s）\n问：%s\n答：%s" % (
            c.get("turn_no") or 0, c.get("ask_type") or "-", c.get("relation") or "-",
            (c.get("text") or "")[:400], ans or "（未抓到）"))
    return "\n\n".join(out)


def _first_sentence(text, limit=60):
    t = re.sub(r"[`*#>]+", "", (text or "").strip())
    t = re.sub(r"\s+", " ", t)
    for seg in re.split(r"(?<=[。！？.!?])\s*|\n", t):
        if seg.strip():
            return seg.strip()[:limit]
    return t[:limit]


# 提问开头常带的过渡语，做标题时没用，去掉才看得清"这一轮到底问什么"
def _tidy_q(text, limit=22):
    from classify import tidy_question
    return tidy_question(text, limit)


def _local_outline(th, turns):
    """没有大模型时的规则大纲：照父子关系排层级，结论优先，其次回答首句。"""
    by_id = {c["id"]: c for c in turns}

    def depth(c):
        d, cur, guard, seen = 0, c, 0, set()
        while guard < 20:
            pid = cur.get("parent_id")
            if not pid or pid not in by_id or pid in seen:
                break
            seen.add(pid)
            cur = by_id[pid]
            d += 1
            guard += 1
        return min(d, 2)

    outline = []
    for i, c in enumerate(turns, 1):
        q = _tidy_q(c.get("text") or "")
        lv = depth(c) + 1
        head = ("%s·" % c["ask_type"] if c.get("ask_type") else "") + q
        point = (c.get("conclusion") or "").strip()
        if not point:
            a = (c.get("answer") or "").strip()
            point = _first_sentence(a) if a else "未展开"
        outline.append({"level": lv, "heading": head, "point": point,
                        "turns": [c.get("turn_no") or i]})
    open_q = ["第%d轮没抓到回答" % (c.get("turn_no") or i)
              for i, c in enumerate(turns, 1) if not (c.get("answer") or "").strip()]
    return {
        "mode": "local",
        "title": th.get("title") or "",
        "intent": _first_sentence(turns[0].get("text") or "", 80),
        "outline": outline,
        "open": open_q[:3],
        "next": "",
    }


def thread_outline(tid, cfg=None, force=True, use_llm=None):
    """把一条线索整理成层级大纲，存进 threads.outline 并返回。"""
    cfg = cfg or load_config()
    th = store.get_thread(tid)
    if not th:
        return {"error": "线索不存在"}
    if not force and th.get("outline"):
        try:
            return json.loads(th["outline"])
        except Exception:
            pass
    turns = store.thread_cards(tid)
    if not turns:
        return {"error": "这条线索里还没有内容"}

    if use_llm is None:
        use_llm = active(cfg)

    data = None
    if use_llm:
        try:
            msg = THREAD_TMPL.format(n=len(turns), blocks=_turn_block(turns))
            data = parse_json(chat([{"role": "system", "content": THREAD_SYS},
                                    {"role": "user", "content": msg}], cfg, temperature=0.2))
            data["mode"] = "llm"
        except Exception as e:
            print("[thread] llm failed, fallback to local:", e)
            data = None
    if data is None:
        data = _local_outline(th, turns)

    data["at"] = store.now_str()
    data["turns"] = len(turns)
    store.set_thread_fields(tid, outline=json.dumps(data, ensure_ascii=False),
                            outline_at=data["at"], intent=(data.get("intent") or "")[:200])
    # 标题还是自动的，就让大模型起的标题覆盖掉"第一句话"
    t = (data.get("title") or "").strip()
    if t and th.get("title_auto"):
        store.set_thread_fields(tid, title=t[:40])
    return data


# ---------------------------------------------------------------- 日报

DAILY_SYS = "你是一个严谨的个人知识管理助手。只做归纳，不编造。只输出 JSON，不要任何解释文字。"

DAILY_TMPL = """以下是我在 {day} 问过 AI 的全部问题（共 {n} 条）。请归纳成一份日报。

{cards}

注意：带「线索<标题>第N轮」标记的卡片属于同一次连续对话（一次追问链），
请优先按线索组织，说明这条线今天推进到了哪里，不要把一条线索拆散。

请严格按这个 JSON 结构输出：
{{
  "headline": "今天的一句话总结，不超过30字",
  "threads": [{{"title": "线索主题（沿用线索标题即可）", "cards": [卡片id], "progress": "这条线今天推进到哪了，一句话"}}],
  "themes": [{{"name": "主题名（4-8字）", "cards": [卡片id], "insight": "这个主题下真正的收获，一句话"}}],
  "cards": [{{"id": 卡片id, "gist": "这张卡的一句话结论，20字内", "tags": ["中文标签1","标签2"],
             "link": {{"to": 另一张卡片id, "type": "延伸|前提|反驳|同主题", "why": "为什么相关，15字内"}}}}],
  "sop": [{{"pattern": "我反复问的一类问题", "suggest": "可以固化成什么模板或清单"}}]
}}

要求：
1. threads 只写今天有 2 轮以上的线索，最多 4 条；
2. themes 最多 4 个，覆盖所有有内容的卡片；
3. cards 里每张卡都要有一条 gist，判断不出的写「待补充」；
4. link 只在这批卡片之间找真正的关系，没有就给 null；
5. sop 只写确实反复出现的，最多 2 条，没有就给空数组。"""


def _card_block(cards, neighbors=None, ans_limit=900):
    out = []
    for c in cards:
        ans = (c.get("answer") or "").replace("\n\n", "\n").strip()
        if len(ans) > ans_limit:
            ans = ans[:ans_limit] + "…"
        nb = ""
        if neighbors and neighbors.get(c["id"]):
            nb = "；".join("#%d" % o["id"] for _, o in neighbors[c["id"]][:3])
        # 追问必须带上"属于哪条线索第几轮"，否则日报里全是断头话
        if c.get("thread_id"):
            pos = "｜线索<%s>第%d轮·%s" % (
                c.get("thread_title") or c["thread_id"][:12],
                c.get("turn_no") or 0, c.get("relation") or "")
        else:
            pos = ""
        out.append(
            "### 卡片 %d（%s / %s%s）\n问：%s\n答：%s\n可能相关的历史卡片：%s"
            % (c["id"], c.get("source") or "?", c.get("weight"), pos,
               c["text"][:400], ans or "（未抓到）", nb or "无")
        )
    return "\n\n".join(out)


def _cluster(cards, limit=4):
    """按标签覆盖度聚类：先统计整批卡片里哪个标签覆盖最多，再逐张归入。"""
    freq = {}
    for c in cards:
        for t in (c.get("tags") or "").split(","):
            if t:
                freq[t] = freq.get(t, 0) + 1
    buckets = {}
    for c in cards:
        tags = [t for t in (c.get("tags") or "").split(",") if t]
        if not tags:
            buckets.setdefault("未分类", []).append(c["id"])
            continue
        key = max(tags, key=lambda t: freq.get(t, 0))
        buckets.setdefault(key, []).append(c["id"])
    ordered = sorted(buckets.items(), key=lambda kv: -len(kv[1]))
    return [{"name": k, "cards": v, "insight": "%d 条相关提问" % len(v)} for k, v in ordered[:limit]]


def _local_digest(day, cards, neighbors):
    """没有大模型时的规则汇总：先按线索归组，再按标签聚类，不做文学加工。"""
    themes = _cluster(cards)
    th_map = {}
    for c in cards:
        if c.get("thread_id"):
            k = c.get("thread_title") or c["thread_id"]
            th_map.setdefault(k, []).append(c["id"])
    threads = [{"title": k, "cards": v, "progress": "今天在 %d 轮追问上推进" % len(v)}
               for k, v in th_map.items() if len(v) >= 2][:4]
    items = []
    for c in cards:
        link = None
        if neighbors.get(c["id"]):
            sim, o = neighbors[c["id"]][0]
            link = {"to": o["id"], "type": "同主题", "why": "相似度 %.2f" % sim}
        items.append({
            "id": c["id"],
            "gist": (c.get("conclusion") or "").strip()[:40] or "待补充",
            "tags": [t for t in (c.get("tags") or "").split(",") if t][:3],
            "link": link,
        })
    return {
        "mode": "local",
        "period": day,
        "headline": "共 %d 条提问，%d 条已抓到回答" % (len(cards), sum(1 for c in cards if c.get("answer"))),
        "threads": threads,
        "themes": themes,
        "cards": items,
        "sop": [],
    }


def run_daily(day=None, use_llm=None, cfg=None, export=True):
    cfg = cfg or load_config()
    day = day or _date.today().isoformat()
    cards = store.cards_between(day, day)
    if not cards:
        return {"mode": "empty", "period": day, "headline": "这天没有提问记录", "themes": [], "cards": [], "sop": []}

    pool = store.live_cards()
    neighbors = {c["id"]: recall(c, pool) for c in cards}
    build_links(cards, pool)

    if use_llm is None:
        use_llm = active(cfg)

    digest = None
    if use_llm:
        try:
            msg = DAILY_TMPL.format(day=day, n=len(cards), cards=_card_block(cards, neighbors))
            raw = chat([{"role": "system", "content": DAILY_SYS},
                        {"role": "user", "content": msg}], cfg)
            digest = parse_json(raw)
            digest["mode"] = "llm"
            digest["period"] = day
        except Exception as e:
            print("[daily] llm failed, fallback to local:", e)
            digest = None

    if digest is None:
        digest = _local_digest(day, cards, neighbors)

    for item in digest.get("cards") or []:
        cid = item.get("id")
        if not cid:
            continue
        if item.get("gist"):
            store.set_field(int(cid), gist=item["gist"])
        lk = item.get("link")
        if isinstance(lk, dict) and lk.get("to"):
            store.add_link(int(cid), int(lk["to"]), lk.get("type") or "同主题",
                           lk.get("why") or "", "llm")

    store.save_digest("daily", day, json.dumps(digest, ensure_ascii=False))
    store.job_set("daily")

    if export:
        try:
            import obsidian
            obsidian.export_all(cfg=cfg)
        except Exception as e:
            print("[daily] export failed:", e)
    return digest


# ---------------------------------------------------------------- 周报

WEEKLY_SYS = DAILY_SYS

WEEKLY_TMPL = """以下是我这一周（{start} ~ {end}）问过 AI 的问题汇总。请生成周报。

{dailies}

本周全部卡片（简要）：
{cards}

请严格按这个 JSON 结构输出：
{{
  "headline": "这一周的一句话总结，不超过40字",
  "threads": [{{"name": "主线名", "cards": [卡片id], "progress": "这条线这周推进到哪了，一句话"}}],
  "patterns": [{{"name": "我反复在问的一类事", "count": 次数, "advice": "说明我卡在哪，一句话"}}],
  "sop": [{{"pattern": "提问模式", "suggest": "固化成什么模板", "ready": true}}],
  "next": ["下周值得做的一件事", "另一件"]
}}

要求：threads 最多 3 条；patterns 只写真的重复出现的；next 最多 2 条，要具体可执行。"""


def run_weekly(end_day=None, use_llm=None, cfg=None, export=True):
    cfg = cfg or load_config()
    end = _date.fromisoformat(end_day) if end_day else _date.today()
    start = end - timedelta(days=6)
    period = "%d-W%02d" % end.isocalendar()[:2]

    cards = store.cards_between(start.isoformat(), end.isoformat())
    if not cards:
        return {"mode": "empty", "period": period, "headline": "这周没有提问记录", "threads": [], "patterns": [], "next": []}

    dailies = []
    for i in range(7):
        d = (start + timedelta(days=i)).isoformat()
        dg = store.get_digest("daily", d)
        if dg and dg.get("content"):
            try:
                j = json.loads(dg["content"])
                dailies.append(
                    "%s ｜ %s\n主题：%s" % (d, j.get("headline", ""),
                                          "、".join(t.get("name", "") for t in j.get("themes") or []))
                )
            except Exception:
                pass

    pool = store.live_cards()
    if use_llm is None:
        use_llm = active(cfg)

    digest = None
    if use_llm:
        try:
            msg = WEEKLY_TMPL.format(
                start=start.isoformat(), end=end.isoformat(),
                dailies="\n".join(dailies) or "（本周没有日报）",
                cards=_card_block(cards, ans_limit=200),
            )
            digest = parse_json(chat([{"role": "system", "content": WEEKLY_SYS},
                                      {"role": "user", "content": msg}], cfg))
            digest["mode"] = "llm"
        except Exception as e:
            print("[weekly] llm failed, fallback:", e)
            digest = None

    if digest is None:
        top = _cluster(cards, 3)
        digest = {
            "mode": "local",
            "headline": "本周 %d 条提问，%d 条有回答" % (len(cards), sum(1 for c in cards if c.get("answer"))),
            "threads": [{"name": t["name"], "cards": t["cards"], "progress": t["insight"]} for t in top],
            "patterns": [], "sop": [], "next": [],
        }

    digest["period"] = period
    digest["range"] = [start.isoformat(), end.isoformat()]
    store.save_digest("weekly", period, json.dumps(digest, ensure_ascii=False))
    store.job_set("weekly")

    if export:
        try:
            import obsidian
            obsidian.export_all(cfg=cfg)
        except Exception as e:
            print("[weekly] export failed:", e)
    return digest


# ---------------------------------------------------------------- 命令行

if __name__ == "__main__":
    import sys
    store.init_db()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "daily"
    if cmd == "daily":
        d = sys.argv[2] if len(sys.argv) > 2 else None
        print(json.dumps(run_daily(d), ensure_ascii=False, indent=2))
    elif cmd == "weekly":
        d = sys.argv[2] if len(sys.argv) > 2 else None
        print(json.dumps(run_weekly(d), ensure_ascii=False, indent=2))
    elif cmd == "links":
        print("new links:", build_links(store.live_cards()))
    else:
        print("usage: python llm.py [daily|weekly|links] [YYYY-MM-DD]")
