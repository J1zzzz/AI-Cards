#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""路径解析：源码运行与 PyInstaller 打包运行时都指向正确的位置。

- DATA_DIR：数据目录。打包后是 exe 所在文件夹（卡片库、配置写在这里，不会随临时解包目录消失）
- RES_DIR ：只读资源目录。打包后是 PyInstaller 的临时解包目录（dashboard.html 在这里）
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    DATA_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    DATA_DIR = os.path.dirname(os.path.abspath(__file__))

RES_DIR = getattr(sys, "_MEIPASS", DATA_DIR)


def find(*names):
    """按「数据目录 → 资源目录」的顺序找文件，返回第一个存在的绝对路径。"""
    for d in (DATA_DIR, RES_DIR):
        p = os.path.join(d, *names)
        if os.path.exists(p):
            return p
    return os.path.join(DATA_DIR, *names)
