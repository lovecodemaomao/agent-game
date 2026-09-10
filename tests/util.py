# -*- coding: utf-8 -*-
"""测试公共工具：宽松加载 docs 下的 demo 报文。

官方 demo 报文存在尾逗号/缺逗号等书写瑕疵（人工编写文档），
真实判题器不会产生此类报文；测试加载时做最小修复，
docs/ 原文件保持只读不动。
"""
import json
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def loose_json_load(path: str):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()
    # 去掉 ] 或 } 前的尾逗号
    text = re.sub(r",(\s*[\]}])", r"\1", text)
    # 补回被误删的、位于 "xxx": 之前的逗号（] 或 " 后直接跟新键）
    text = re.sub(r"(\])(\s*\n\s*\"[^\"]+\")", r"\1,\2", text)
    text = re.sub(r"(\"\))(,\s*\n\s*\")", r"\1\2", text)
    return json.loads(text)


def load_demo_request():
    return loose_json_load(os.path.join(ROOT, "docs", "request.txt"))
