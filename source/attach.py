#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""附件：落盘、抽正文、按 sha1 去重。

设计原则：
- 原件永远留在本地，不进数据库（数据库只存元信息和抽出来的文字），
  这样库可以随便备份，几十兆的 PDF 也不会把 cards.db 撑坏。
- 同一个文件重复喂给 AI 只存一份（文件名用 sha1），但可以挂到多张卡上。
- 抽不出文字不算失败（扫描版 PDF、纯图片），记为 kind 并在界面上标"仅存原件"。
"""

import hashlib
import html
import mimetypes
import os
import re
import zipfile

import paths

FILES_DIR = os.path.join(paths.DATA_DIR, "files")
TEXT_CAP = 60000          # 单份附件最多留多少字正文，够复习和搜索，又不至于把库撑大
SNIPPET = 240             # 卡片上一句话摘要的长度

TEXT_EXT = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".log",
            ".py", ".js", ".ts", ".html", ".htm", ".xml", ".yaml", ".yml", ".ini"}
IMG_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
DOC_EXT = {".docx", ".doc", ".pdf", ".pptx", ".xlsx"}

BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def safe_name(name, limit=80):
    """只用于展示和导出，磁盘上用的是 sha1，所以这里只要不出现路径分隔符即可。"""
    n = BAD_CHARS.sub("_", (name or "").strip()) or "附件"
    if len(n) > limit:
        base, ext = os.path.splitext(n)
        n = base[:limit - len(ext)] + ext
    return n


def kind_of(name, mime=""):
    ext = os.path.splitext(name or "")[1].lower()
    mime = (mime or "").lower()
    if ext in IMG_EXT or mime.startswith("image/"):
        return "image"
    if ext == ".pdf" or mime == "application/pdf":
        return "pdf"
    if ext in (".docx", ".doc", ".pptx", ".xlsx") or "officedocument" in mime or "msword" in mime:
        return "office"
    if ext in TEXT_EXT or mime.startswith("text/"):
        return "text"
    return "other"


def _decode(raw):
    for enc in ("utf-8", "utf-8-sig", "gb18030", "big5", "latin-1"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _docx_text(path):
    """docx/pptx/xlsx 都是 zip + xml，不引第三方库也能拿到正文。"""
    out = []
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            targets = []
            if "word/document.xml" in names:
                targets = ["word/document.xml"]
            elif "ppt/slides/slide1.xml" in names:
                targets = sorted([n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)],
                                 key=lambda s: int(re.findall(r"\d+", s)[0]))
            elif "xl/sharedStrings.xml" in names:
                targets = ["xl/sharedStrings.xml"]
            for t in targets:
                xml = _decode(z.read(t))
                xml = re.sub(r"</w:p>|</a:p>|</row>", "\n", xml)
                xml = re.sub(r"<w:tab[^>]*/>", "\t", xml)
                xml = re.sub(r"<[^>]+>", "", xml)
                out.append(html.unescape(xml))
    except Exception:
        return ""
    return _tidy("\n".join(out))


def _pdf_text(path):
    try:
        from pypdf import PdfReader
    except Exception:
        return ""
    try:
        reader = PdfReader(path)
        buf, total = [], 0
        for page in reader.pages:
            try:
                t = page.extract_text() or ""
            except Exception:
                t = ""
            if t.strip():
                buf.append(t)
                total += len(t)
            if total >= TEXT_CAP:
                break
        return _tidy("\n".join(buf))
    except Exception:
        return ""


def _tidy(t):
    t = (t or "").replace("\x00", "")
    t = re.sub(r"[ \t]+\n", "\n", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def extract_text(path, kind):
    if kind == "text":
        try:
            with open(path, "rb") as f:
                return _tidy(_decode(f.read()))[:TEXT_CAP]
        except Exception:
            return ""
    if kind == "pdf":
        return _pdf_text(path)[:TEXT_CAP]
    if kind == "office":
        return _docx_text(path)[:TEXT_CAP]
    return ""


def snippet(text, limit=SNIPPET):
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return t[:limit] + ("…" if len(t) > limit else "")


def save_bytes(data, name, mime="", card_id=0, origin="upload"):
    """落盘并抽正文，返回可直接入库的字段。失败时返回 None。"""
    if not data:
        return None
    name = safe_name(name)
    mime = mime or (mimetypes.guess_type(name)[0] or "")
    kind = kind_of(name, mime)
    sha = hashlib.sha1(data).hexdigest()
    ext = os.path.splitext(name)[1].lower()
    if ext not in TEXT_EXT | IMG_EXT | DOC_EXT:
        ext = mimetypes.guess_extension(mime) or ext
    sub = os.path.join(FILES_DIR, sha[:2])
    path = os.path.join(sub, sha + ext)
    try:
        if not os.path.exists(path):
            os.makedirs(sub, exist_ok=True)
            with open(path, "wb") as f:
                f.write(data)
    except Exception:
        return None

    text = extract_text(path, kind)
    return {
        "card_id": int(card_id or 0),
        "sha1": sha,
        "name": name,
        "mime": mime,
        "kind": kind,
        "size": len(data),
        "path": os.path.relpath(path, paths.DATA_DIR).replace("\\", "/"),
        "remote": "",
        "text": text,
        "origin": origin,
    }


def save_remote(name, url, card_id=0):
    """站点把文件传到云端了，页面里只剩名字和预览图 —— 只记线索，不假装有原件。"""
    name = safe_name(name or "附件")
    if not url and not name:
        return None
    return {
        "card_id": int(card_id or 0),
        "sha1": hashlib.sha1((name + "|" + (url or "")).encode("utf-8")).hexdigest(),
        "name": name,
        "mime": mimetypes.guess_type(name)[0] or "",
        "kind": kind_of(name),
        "size": 0,
        "path": "",
        "remote": url or "",
        "text": "",
        "origin": "remote",
    }


def abs_path(rel):
    """数据库里存的是相对 DATA_DIR 的路径，取文件时再拼回来，换电脑也不会失效。"""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if not rel:
        return ""
    p = os.path.normpath(os.path.join(paths.DATA_DIR, rel))
    root = os.path.normpath(paths.DATA_DIR)
    if not p.startswith(root + os.sep):
        return ""      # 防目录穿越
    return p if os.path.exists(p) else ""
