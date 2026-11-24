#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""权值判定：纯规则，快、免费、可解释。判错了在卡片上手动改即可。"""

import re

TECH_KW = [
    "代码", "报错", "异常", "函数", "变量", "接口", "部署", "数据库", "索引", "算法",
    "架构", "框架", "并发", "线程", "内存", "性能", "优化", "重构", "测试", "调试",
    "编译", "依赖", "版本", "命令行", "正则", "协议", "缓存", "队列", "分布式", "微服务",
    "爬虫", "脚本", "配置", "环境", "服务器", "日志", "前端", "后端", "对象", "继承",
    "加密", "证书", "域名", "解析", "打包", "构建", "容器", "虚拟机", "端口", "防火墙",
    "sql", "api", "docker", "nginx", "linux", "git", "python", "java", "javascript",
    "typescript", "react", "vue", "node", "redis", "kafka", "k8s", "json", "http",
]
WORK_KW = [
    "方案", "策略", "报价", "合同", "条款", "汇报", "复盘", "指标", "流程", "绩效",
    "招人", "面试", "简历", "客户", "预算", "成本", "毛利", "转化", "增长", "定位",
    "品牌", "股权", "税务", "发票", "合规", "法务", "排期", "需求", "原型", "竞品", "定价",
]
LIFE_KW = [
    "天气", "菜谱", "做饭", "好吃", "减肥", "健身", "跑步", "心情", "难过", "无聊",
    "电影", "电视剧", "音乐", "游戏", "旅游", "酒店", "机票", "穿搭", "衣服", "发型",
    "护肤", "化妆", "感冒", "发烧", "咳嗽", "宠物", "生日", "礼物", "星座", "恋爱",
    "分手", "相亲", "租房", "买车", "快递", "外卖", "附近", "推荐", "想吃", "去哪",
]
# 方法论/认知类：这类问题没有技术词，但恰恰最值得长期留
MIND_KW = [
    "机制", "体系", "沉淀", "原理", "本质", "逻辑", "取舍", "权衡", "边界", "标准",
    "判断", "决策", "反思", "认知", "方法论", "设计", "为什么", "长期", "习惯",
    "效率", "体系化", "规律", "原则", "策略",
]

TECH_SET, WORK_SET, LIFE_SET = set(TECH_KW), set(WORK_KW), set(LIFE_KW)

# 这些词参与打分，但不应成为标签（否则 Obsidian 图谱和日报分组会被"为什么"这类词污染）
TAG_STOP = {
    "为什么", "怎么", "如何", "什么", "是否", "设计", "判断", "决策", "长期",
    "习惯", "反思", "认知", "方法论", "思路", "方案", "问题", "推荐", "附近",
}
MIND_SET = set(MIND_KW)


def score_text(text):
    """返回 (score 0-100, tags, weight)"""
    low = (text or "").lower()
    s, hits = 20, []

    for kw in TECH_SET:
        if kw in low:
            s += 11
            hits.append(kw)
    for kw in WORK_SET:
        if kw in low:
            s += 7
            hits.append(kw)
    for kw in MIND_SET:
        if kw in low:
            s += 9
            hits.append(kw)
    for kw in LIFE_SET:
        if kw in low:
            s -= 13

    n = len(text or "")
    if n >= 120:
        s += 10
    elif n >= 60:
        s += 6
    elif n < 15:
        s -= 10

    if "```" in (text or "") or re.search(r"[A-Za-z_]{3,}\(", text or ""):
        s += 12
    if len(re.findall(r"\b[A-Za-z]{3,}\b", text or "")) > 4:
        s += 8
    if (text or "").rstrip().endswith(("?", "？")):
        s += 3

    s = max(0, min(100, s))
    weight = "high" if s >= 58 else ("mid" if s >= 30 else "low")

    seen, tags = set(), []
    for kw in hits:
        if kw in TAG_STOP or kw in seen:
            continue
        seen.add(kw)
        tags.append(kw)
    if not tags:
        tags = ["待归类"]
    return s, ",".join(tags[:3]), weight


# ---------------------------------------------------------------- 追问的类型与关系
# 追问链要有结构，第一步是知道"这一轮到底在问什么"。
# 纯规则判断，判错可以手改，但比什么都不标好。

ASK_RULES = [
    ("定义", ("是什么", "什么是", "定义", "概念", "含义", "指的是", "介绍一下", "介绍", "解释一下")),
    ("对比", ("区别", "对比", "差异", "相比", "优劣", "优缺点", "哪个好", "vs", "比较")),
    ("原因", ("为什么", "为何", "原因", "怎么会", "凭什么", "导致")),
    ("举例", ("举例", "例子", "示例", "实例", "案例", "举个")),
    ("应用", ("应用", "落地", "场景", "实践", "用来", "能做什么", "用在哪", "怎么用")),
    ("方法", ("怎么", "如何", "怎样", "步骤", "流程", "实现", "做法", "写法", "配一下", "写一个")),
    ("评价", ("值得", "靠谱", "好不好", "行不行", "评价", "风险", "坑")),
]

CONT_WORDS = ("那", "这", "它", "他", "她", "其", "该", "上面", "刚才", "继续", "接着",
              "然后", "还有", "另外", "再", "所以", "因此", "那么")


def ask_type(text):
    """这一轮问的是哪一类问题。返回 定义/对比/原因/举例/应用/方法/评价/延伸 之一。"""
    t = (text or "").strip()
    low = t.lower()
    for name, kws in ASK_RULES:
        if any(k in low for k in kws):
            return name
    return "延伸"


def is_continuation(text):
    """像"那它的标注噪音怎么处理"这种，明显是在承接上一轮。"""
    t = (text or "").strip()
    return bool(t) and t[:2].startswith(CONT_WORDS)


def relation_of(text, has_parent=True):
    """这一轮和上一轮是什么关系：起点 / 承接 / 追问 / 延伸。"""
    if not has_parent:
        return "起点"
    t = (text or "").strip()
    if is_continuation(t):
        return "承接"
    if len(t) <= 14:
        return "追问"
    return "延伸"


# 提问开头的过渡语，做标题时没用，去掉才看得清"这一轮到底问什么"
FILLER = ("这里我还想问你一个问题", "我想问的是", "我想问一下", "我还想问一个问题",
          "我还想问", "我知道了", "还有一个问题", "那么", "所以", "另外",
          "好的", "可以", "是的", "嗯", "对", "请问", "然后")


def tidy_question(text, limit=24):
    """把口语化的提问收成能当标题的一句话。幂等：收拾过的再收拾一次结果不变。"""
    t = (text or "").replace("\n", " ").strip()
    changed = True
    while changed:
        changed = False
        for w in FILLER:
            if t.startswith(w) and len(t) > len(w):
                nxt = t[len(w):len(w) + 1]
                if nxt in "，。：:、, ":
                    t = t[len(w):].lstrip("，。：:、, ")
                    changed = True
                    break
    t = t.rstrip("？?。！!… ").strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def bigrams(text):
    """中文按字二元组，英文按词，用于本地相似度召回（不依赖向量库）。"""
    t = (text or "").lower()
    zh = re.sub(r"[^\u4e00-\u9fff]", "", t)
    grams = {zh[i:i + 2] for i in range(len(zh) - 1)}
    grams |= {w for w in re.findall(r"[a-z]{3,}", t)}
    return grams


def similarity(a, b):
    ga, gb = bigrams(a), bigrams(b)
    if not ga or not gb:
        return 0.0
    inter = len(ga & gb)
    return inter / min(len(ga), len(gb))


def score_answer(text):
    """回答本身的价值信号。短问题 + 长回答，往往比长问题 + 一句话更值得留，
    所以打分不能只看问题。"""
    t = text or ""
    n = len(t)
    s = 0
    if n >= 900:
        s += 30
    elif n >= 500:
        s += 22
    elif n >= 250:
        s += 14
    elif n >= 120:
        s += 8
    if "```" in t:
        s += 12
    if n >= 120 and len(re.findall(r"^\s*(?:[-*+]|\d+[.)])\s", t, re.M)) >= 3:
        s += 6
    return min(60, s)
