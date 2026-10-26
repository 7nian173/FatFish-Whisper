"""结构化校验 mermaid 渲染产物（不依赖视觉模型）。

要点：mermaid-cli 12 输出的 SVG 把标签文字放在 <foreignObject> 内的 HTML
<span>/<div> 里，而不是 <text>/<tspan>，所以必须按 HTML 剥标签取文字。

检查项：
1. SVG 是否合法 XML、根元素 svg、有 viewBox
2. 从 foreignObject(HTML) + text/tspan(SVG) 两路取文字，确认预期关键词都在
3. 乱码检测（U+FFFD 替换符 / U+25A1 豆腐块）
4. PNG 头是否合法、宽高是否合理
5. 统计节点数（class 含 "node" 的 <g>）
"""

import io
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET

sys.stdout.reconfigure(encoding="utf-8")

DIR = r"E:\QQBOT\开发\AGENT\开发空间\FatFish-Whisper 0.1\docs\specs\diagrams"

CASES = [
    (
        "fig2-1-architecture-overview",
        [
            "Adapter 探针层",
            "Core-Engine 情绪机",
            "存储层 · 全局区",
            "存储层 · 分房区",
            "UserAffinity",
            "BotMood",
            "UserState",
            "memories",
            "trigger_type",
            "chat_scope",
        ],
    ),
    (
        "fig2-2-2-emotion-state-machine",
        [
            "读 UserState.emotion_current",
            "每30min向50靠近10%",
            "0.2→0.1→0.05",
            "二次夹取",
            "CRISIS≤15",
            "写 UserState",
            "risk=True 时 level 强制 CRISIS",
            "whitelist 用户 CRISIS 降级为 L2",
        ],
    ),
    (
        "fig2-2-3-decision-tree",
        [
            "消息进入",
            "IngressDeduper",
            "白名单短路",
            "Track A 词典判定",
            "target=0",
            "LLM 仲裁",
            "五档打分",
            "auditor.audit",
            "update_emotion",
        ],
    ),
]

TAG = re.compile(r"<[^>]+>")
ENT = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&quot;": '"', "&#39;": "'", "&nbsp;": " "}


def unescape(s):
    for k, v in ENT.items():
        s = s.replace(k, v)
    return s


def png_size(path):
    with open(path, "rb") as f:
        head = f.read(24)
    if head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return struct.unpack(">II", head[16:24])


def collect_text(svg_path):
    raw = io.open(svg_path, encoding="utf-8").read()
    chunks = []

    # 1) foreignObject 内的 HTML 文字（mermaid 12 主路径）
    for body in re.findall(r"<foreignObject\b[^>]*>(.*?)</foreignObject>", raw, re.S):
        # <br> 换成空格，避免单词粘连
        body = re.sub(r"<br\s*/?>", " ", body)
        txt = unescape(TAG.sub("", body))
        txt = re.sub(r"\s+", " ", txt).strip()
        if txt:
            chunks.append(txt)

    # 2) 传统 <text>/<tspan>（兼容旧版本 mermaid）
    tree = ET.parse(svg_path)
    root = tree.getroot()
    for el in root.iter():
        if el.tag.split("}")[-1] in ("text", "tspan") and el.text:
            t = el.text.strip()
            if t:
                chunks.append(t)

    return root, chunks, raw


fail = 0
for name, expect in CASES:
    svg = os.path.join(DIR, name + ".svg")
    png = os.path.join(DIR, name + ".png")
    print("=" * 64)
    print(name)

    if not os.path.exists(svg):
        print("  SVG MISSING:", svg)
        fail += 1
        continue

    try:
        root, chunks, raw = collect_text(svg)
    except Exception as exc:
        print("  SVG PARSE FAIL:", exc)
        fail += 1
        continue

    tag = root.tag.split("}")[-1]
    print(f"  svg root=<{tag}> viewBox={root.get('viewBox')}")
    joined = "\n".join(chunks)
    print(f"  text chunks={len(chunks)} chars={len(joined)}")

    nodes = len(re.findall(r'<g[^>]*class="[^"]*\bnode\b', raw))
    edges = len(re.findall(r'<path[^>]*class="[^"]*flowchart-link', raw)) or len(
        re.findall(r'class="[^"]*transition', raw)
    )
    print(f"  nodes={nodes} edges~={edges}")

    missing = [k for k in expect if k not in joined]
    if missing:
        print("  MISSING TEXT:", missing)
        fail += 1
    else:
        print(f"  all {len(expect)} expected labels present")

    for bad in ("\ufffd", "\u25a1"):
        if bad in joined:
            print(f"  GARBLE detected: U+{ord(bad):04X}")
            fail += 1

    size = png_size(png) if os.path.exists(png) else None
    if size is None:
        print("  PNG MISSING/INVALID")
        fail += 1
    else:
        w, h = size
        ok = w > 300 and h > 300
        print(f"  png {w}x{h} {'ok' if ok else 'TOO SMALL'}")
        if not ok:
            fail += 1

print("=" * 64)
print("FAILURES:", fail)
sys.exit(1 if fail else 0)
