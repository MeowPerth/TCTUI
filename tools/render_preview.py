#!/usr/bin/env python3
"""Render a tctui ANSI dump (tctui ui --dump) to a PNG via headless Chromium.

Dev tool only: takes the escape-coded frame, paints it on a canvas cell grid so
CJK glyphs line up, then asks Chromium for a 2x screenshot.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
import sys
import tempfile

SGR = re.compile(r"\x1b\[([0-9;]*)m")

FG = {
    30: "#1b1f23", 31: "#f97583", 32: "#7ee787", 33: "#e3b341", 34: "#79c0ff",
    35: "#d2a8ff", 36: "#56d4dd", 37: "#c9d1d9", 90: "#6e7681", 39: "#c9d1d9",
}
BG = {
    40: "#1b1f23", 41: "#8e1519", 42: "#1b4721", 43: "#7a5c00", 44: "#14406e",
    45: "#4b2a6b", 46: "#0d4a52", 47: "#c9d1d9", 49: "#0d1117",
    100: "#21262d", 101: "#8e1519", 102: "#1b4721", 103: "#7a5c00", 104: "#0b3a68",
    105: "#4b2a6b", 106: "#0d4a52", 107: "#c9d1d9",
}


def parse_frame(text):
    """-> list of rows, each row = [(char, fg, bg, bold), ...]"""
    fg, bg, bold = FG[39], BG[49], False
    rows = []
    for line in text.split("\n"):
        cells = []
        pos = 0
        for m in SGR.finditer(line):
            for ch in line[pos:m.start()]:
                cells.append((ch, fg, bg, bold))
            params = [int(p) for p in m.group(1).split(";") if p != ""] or [0]
            i = 0
            while i < len(params):
                p = params[i]
                if p == 0:
                    fg, bg, bold = FG[39], BG[49], False
                elif p == 1:
                    bold = True
                elif p == 22:
                    bold = False
                elif p in FG:
                    fg = FG[p]
                elif p in BG:
                    bg = BG[p]
                i += 1
            pos = m.end()
        for ch in line[pos:]:
            cells.append((ch, fg, bg, bold))
        while cells and cells[-1][0] == " " and cells[-1][2] == BG[49]:
            cells.pop()
        rows.append(cells)
    while rows and not rows[-1]:
        rows.pop()
    return rows


def build_html(rows, cell_w=9, cell_h=18, title="tctui"):
    cols = max((sum(2 if _wide(c) else 1 for c, *_ in r) for r in rows), default=80)
    pad = 16
    w = cols * cell_w + pad * 2
    h = len(rows) * cell_h + pad * 2
    data = [[[c, fg, bg, bold] for (c, fg, bg, bold) in row] for row in rows]
    import json
    payload = json.dumps(data, ensure_ascii=False)
    return """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
 html,body{{margin:0;padding:0;background:#0d1117;}}
 #wrap{{position:relative;width:{w}px;height:{h}px;background:#0d1117;}}
 canvas{{display:block;}}
</style></head><body>
<div id="wrap"><canvas id="c" width="{w}" height="{h}"></canvas></div>
<script>
const CW={cw}, CH={ch}, PAD={pad};
const rows={payload};
const cv=document.getElementById('c'), ctx=cv.getContext('2d');
ctx.textBaseline='top';
const LEFT_BLOCK={{'▏':0.125,'▎':0.25,'▍':0.375,'▌':0.5,'▋':0.625,'▊':0.75,'▉':0.875}};
const BOTTOM_BLOCK={{'▁':0.15,'▂':0.3,'▃':0.45,'▄':0.6,'▅':0.72,'▆':0.84,'▇':0.93}};
function drawBlock(ch,x,y,fg,cw,chh){{
  ctx.fillStyle=fg;
  if(ch==='█'){{ ctx.fillRect(x,y+2,cw,chh-4); return true; }}
  if(LEFT_BLOCK[ch]!==undefined){{ ctx.fillRect(x,y+2,cw*LEFT_BLOCK[ch],chh-4); return true; }}
  if(BOTTOM_BLOCK[ch]!==undefined){{ const f=BOTTOM_BLOCK[ch]; ctx.fillRect(x,y+2+ (chh-4)*(1-f),cw,(chh-4)*f); return true; }}
  if(ch==='░'){{ ctx.save(); ctx.globalAlpha=0.28; ctx.fillRect(x,y+2,cw,chh-4); ctx.restore(); return true; }}
  return false;
}}
function drawChar(ch,x,y,fg,bold){{
  ctx.fillStyle=fg;
  ctx.font=(bold?'bold ':'')+'15px "DejaVu Sans Mono","Droid Sans Fallback","Noto Sans Mono",monospace';
  ctx.fillText(ch,x,y+1);
}}
for(let r=0;r<rows.length;r++){{
  let col=0;
  for(const cell of rows[r]){{
    const ch=cell[0], fg=cell[1], bg=cell[2], bold=cell[3];
    const wide=/[\\u1100-\\u115F\\u2E80-\\uA4CF\\uAC00-\\uD7A3\\uF900-\\uFAFF\\uFE30-\\uFE4F\\uFF00-\\uFF60\\uFFE0-\\uFFE6]/.test(ch);
    const cw=wide?2:1;
    if(bg!=='#0d1117'){{
      ctx.fillStyle=bg; ctx.fillRect(PAD+col*CW,PAD+r*CH,cw*CW,CH);
    }}
    if(ch!==' '){{
      if(!drawBlock(ch,PAD+col*CW,PAD+r*CH,fg,cw*CW,CH)) drawChar(ch,PAD+col*CW,PAD+r*CH,fg,bold);
    }}
    col+=cw;
  }}
}}
</script></body></html>""".format(w=w, h=h, cw=cell_w, ch=cell_h, pad=pad, payload=payload, title=html.escape(title))


def _wide(ch):
    import unicodedata
    return unicodedata.east_asian_width(ch) in ("W", "F")


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("ansi_file")
    ap.add_argument("-o", "--out", default="preview.png")
    ap.add_argument("--scale", type=int, default=2)
    ap.add_argument("--title", default="tctui preview")
    args = ap.parse_args(argv)
    with open(args.ansi_file, "r", encoding="utf-8", errors="replace") as fh:
        rows = parse_frame(fh.read())
    page = build_html(rows, title=args.title)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".html", delete=False, encoding="utf-8")
    tmp.write(page)
    tmp.close()
    cols = max((sum(2 if _wide(c) else 1 for c, *_ in r) for r in rows), default=80)
    w = cols * 9 + 32
    h = len(rows) * 18 + 32
    cmd = ["chromium", "--headless", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
           "--force-device-scale-factor=%d" % args.scale,
           "--window-size=%d,%d" % (w, h),
           "--virtual-time-budget=3000",
           "--screenshot=" + os.path.abspath(args.out),
           "file://" + tmp.name]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if res.returncode != 0:
        sys.stderr.write(res.stderr.decode("utf-8", "replace")[-2000:])
        return 1
    print("wrote %s (%dx%d @%dx)" % (args.out, w, h, args.scale))
    return 0


if __name__ == "__main__":
    sys.exit(main())
