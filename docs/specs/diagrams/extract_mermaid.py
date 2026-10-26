"""从 V5.0-core-spec.md 抽出 mermaid 代码块，写成独立 .mmd 文件供 mmdc 渲染。"""

import io
import os
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

SPEC = r"E:\QQBOT\开发\AGENT\开发空间\FatFish-Whisper 0.1\docs\specs\V5.0-core-spec.md"
OUT = r"E:\QQBOT\开发\AGENT\开发空间\FatFish-Whisper 0.1\docs\specs\diagrams"

NAMES = ["fig2-2-2-emotion-state-machine", "fig2-2-3-decision-tree"]

text = io.open(SPEC, encoding="utf-8").read()
blocks = re.findall(r"```mermaid\n(.*?)```", text, re.S)
print("found mermaid blocks:", len(blocks))

os.makedirs(OUT, exist_ok=True)
for name, body in zip(NAMES, blocks):
    p = os.path.join(OUT, name + ".mmd")
    io.open(p, "w", encoding="utf-8", newline="\n").write(body.rstrip() + "\n")
    print("wrote", p, len(body), "chars")

if len(blocks) != len(NAMES):
    print("WARNING: block count mismatch", len(blocks), len(NAMES))
