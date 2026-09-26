#!/usr/bin/env python3
"""重新生成根 README 的全部配图（docs/*.svg）。

用法：python3 docs/diagrams.py            # 写出全部 SVG
      python3 docs/diagrams.py 名字 ...   # 只写出指定的几张

只用标准库。所有图共用一套样式：一个主色（蓝）、三个语义色（绿 = 成功、
琥珀 = 要注意、红 = 停下）、系统中文字体、Lucide 风格的线性图标；
配色在深色模式下自动切换（SVG 内的 prefers-color-scheme 媒体查询）。
"""
from __future__ import annotations

import html
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent

FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI','PingFang SC','Hiragino Sans GB',"
        "'Microsoft YaHei','Noto Sans CJK SC',Helvetica,Arial,sans-serif")
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,'PingFang SC','Microsoft YaHei',monospace"

# 语义色：浅色模式 fill / stroke / 强调色，深色模式同样三项
TONES = {
    'gray':   ('#f6f8fa', '#d8dee4', '#57606a', '#161b22', '#30363d', '#8b949e'),
    'blue':   ('#eef4ff', '#c7d7fe', '#2f5fdb', '#12203a', '#2b4a86', '#79a6ff'),
    'green':  ('#ecfdf3', '#b7e4c7', '#1a7f37', '#0f2a1a', '#1f5a33', '#4ad07a'),
    'amber':  ('#fff8e6', '#f5d78a', '#9a6700', '#2e2410', '#6b4f12', '#e3b341'),
    'red':    ('#fff1f0', '#f7b9b4', '#cf222e', '#301416', '#7a2a2e', '#ff7b72'),
    'purple': ('#f6f1ff', '#d6c7f5', '#6e40c9', '#231a36', '#4a3480', '#b083f0'),
}

STYLE = """
text{font-family:%(font)s;fill:#1f2328;font-size:13px}
.h{font-size:15px;font-weight:600}.hb{font-size:17px;font-weight:700}
.s{font-size:13px;fill:#57606a}.xs{font-size:12px;fill:#6f7781}
.mono{font-family:%(mono)s;font-size:12px;fill:#57606a}
.w{fill:#ffffff;font-weight:600}
.bg{fill:#ffffff}
.line{stroke:#8c959f;stroke-width:1.6;fill:none;stroke-linecap:round;stroke-linejoin:round}
.line.soft{stroke:#c9d1d9}
.ar{stroke:#8c959f;stroke-width:1.6;fill:none;marker-end:url(#ah);stroke-linecap:round;stroke-linejoin:round}
.ar.blue{stroke:#2f5fdb;marker-end:url(#ah-blue)}.ar.green{stroke:#1a7f37;marker-end:url(#ah-green)}
.ar.red{stroke:#cf222e;marker-end:url(#ah-red)}.ar.amber{stroke:#9a6700;marker-end:url(#ah-amber)}
.ar.dash{stroke-dasharray:5 4}
.ic{fill:none;stroke-width:1.9;stroke-linecap:round;stroke-linejoin:round}
%(tones)s
@media (prefers-color-scheme: dark){
text{fill:#e6edf3}.s{fill:#a3adb8}.xs{fill:#8b949e}.mono{fill:#a3adb8}.w{fill:#ffffff}
.bg{fill:#0d1117}
.line{stroke:#6e7681}.line.soft{stroke:#30363d}
.ar{stroke:#6e7681}
.ar.blue{stroke:#79a6ff}.ar.green{stroke:#4ad07a}.ar.red{stroke:#ff7b72}.ar.amber{stroke:#e3b341}
%(dtones)s
}
"""


def _tone_css():
    light, dark = [], []
    for name, (f, s, a, df, ds, da) in TONES.items():
        light.append(f".c-{name}{{fill:{f};stroke:{s};stroke-width:1}}"
                     f".f-{name}{{fill:{a}}}.k-{name}{{stroke:{a}}}.t-{name}{{fill:{a}}}"
                     f".ic-{name}{{stroke:{a}}}.sf-{name}{{fill:{f}}}.ss-{name}{{stroke:{s}}}")
        dark.append(f".c-{name}{{fill:{df};stroke:{ds}}}"
                    f".f-{name}{{fill:{da}}}.k-{name}{{stroke:{da}}}.t-{name}{{fill:{da}}}"
                    f".ic-{name}{{stroke:{da}}}.sf-{name}{{fill:{df}}}.ss-{name}{{stroke:{ds}}}")
    return '\n'.join(light), '\n'.join(dark)


# ---------------------------------------------------------------- 图标（Lucide 风格，24×24）
ICONS = {
    'check': ['M20 6 9 17l-5-5'],
    'x': ['M18 6 6 18', 'm6 6 12 12'],
    'clock': ['<circle cx="12" cy="12" r="10"/>', 'M12 6v6l4 2'],
    'alarm': ['<circle cx="12" cy="13" r="8"/>', 'M12 9v4l2 2', 'M5 3 2 6', 'm22 6-3-3'],
    'monitor': ['<rect x="2" y="3" width="20" height="14" rx="2"/>', 'M8 21h8', 'M12 17v4'],
    'laptop': ['M20 16V7a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v9m16 0H4m16 0 1.28 2.55a1 1 0 0 1-.9 1.45H3.62a1 1 0 0 1-.9-1.45L4 16'],
    'search': ['<circle cx="11" cy="11" r="8"/>', 'm21 21-4.3-4.3'],
    'coins': ['<circle cx="8" cy="8" r="6"/>', 'M18.09 10.37A6 6 0 1 1 10.34 18', 'M7 6h1v4', 'm16.71 13.88.7.71-2.82 2.82'],
    'database': ['<ellipse cx="12" cy="5" rx="9" ry="3"/>', 'M3 5v14a9 3 0 0 0 18 0V5', 'M3 12a9 3 0 0 0 18 0'],
    'file': ['M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z', 'M14 2v4a2 2 0 0 0 2 2h4', 'M10 9H8', 'M16 13H8', 'M16 17H8'],
    'shield': ['M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z'],
    'shield-check': ['M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z', 'm9 12 2 2 4-4'],
    'key': ['m15.5 7.5 2.3 2.3a1 1 0 0 0 1.4 0l2.1-2.1a1 1 0 0 0 0-1.4L19 4', 'm21 2-9.6 9.6', '<circle cx="7.5" cy="15.5" r="5.5"/>'],
    'lock': ['<rect x="3" y="11" width="18" height="11" rx="2"/>', 'M7 11V7a5 5 0 0 1 10 0v4'],
    'user': ['M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2', '<circle cx="12" cy="7" r="4"/>'],
    'user-check': ['M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2', '<circle cx="9" cy="7" r="4"/>', 'm16 11 2 2 4-4'],
    'smile': ['<circle cx="12" cy="12" r="10"/>', 'M8 14s1.5 2 4 2 4-2 4-2', 'M9 9h.01', 'M15 9h.01'],
    'meh': ['<circle cx="12" cy="12" r="10"/>', 'M8 15h8', 'M9 9h.01', 'M15 9h.01'],
    'pointer': ['M4.04 4.69a.5.5 0 0 1 .65-.65l16 6.5a.5.5 0 0 1-.06.95l-6.13 1.58a2 2 0 0 0-1.43 1.43l-1.58 6.13a.5.5 0 0 1-.95.06z'],
    'send': ['M14.5 9.5 21 3', 'M21 3 14 21a.5.5 0 0 1-.9 0l-2.6-6.5L4 11.9a.5.5 0 0 1 0-.9Z'],
    'hourglass': ['M5 22h14', 'M5 2h14', 'M17 22v-4.2a2 2 0 0 0-.6-1.4L12 12l-4.4 4.4A2 2 0 0 0 7 17.8V22', 'M7 2v4.2a2 2 0 0 0 .6 1.4L12 12l4.4-4.4A2 2 0 0 0 17 6.2V2'],
    'refresh': ['M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8', 'M21 3v5h-5', 'M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16', 'M8 16H3v5'],
    'bell': ['M10.3 21a2 2 0 0 0 3.4 0', 'M3.3 15.3A1 1 0 0 0 4 17h16a1 1 0 0 0 .7-1.7C19.4 14 18 12.5 18 8A6 6 0 0 0 6 8c0 4.5-1.4 6-2.7 7.3'],
    'alert': ['m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3', 'M12 9v4', 'M12 17h.01'],
    'help': ['<circle cx="12" cy="12" r="10"/>', 'M9.1 9a3 3 0 0 1 5.8 1c0 2-3 3-3 3', 'M12 17h.01'],
    'eye': ['M2.06 12.35a1 1 0 0 1 0-.7 10.75 10.75 0 0 1 19.88 0 1 1 0 0 1 0 .7 10.75 10.75 0 0 1-19.88 0', '<circle cx="12" cy="12" r="3"/>'],
    'calendar': ['<rect x="3" y="4" width="18" height="18" rx="2"/>', 'M16 2v4', 'M8 2v4', 'M3 10h18'],
    'globe': ['<circle cx="12" cy="12" r="10"/>', 'M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20', 'M2 12h20'],
    'book': ['M12 7v14', 'M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z'],
    'zap': ['M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z'],
    'moon': ['M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z'],
    'history': ['M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8', 'M3 3v5h5', 'M12 7v5l4 2'],
    'folder': ['M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z'],
    'terminal': ['M12 19h8', 'm4 17 6-6-6-6'],
    'bot': ['M12 8V4H8', '<rect x="4" y="8" width="16" height="12" rx="2"/>', 'M2 14h2', 'M20 14h2', 'M15 13v2', 'M9 13v2'],
    'download': ['M12 15V3', 'M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4', 'm7 10 5 5 5-5'],
    'pause': ['<rect x="14" y="4" width="4" height="16" rx="1"/>', '<rect x="6" y="4" width="4" height="16" rx="1"/>'],
    'play': ['M6 4v16l14-8Z'],
    'wrench': ['M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z'],
    'layers': ['M12.83 2.18a2 2 0 0 0-1.66 0L2.6 6.08a1 1 0 0 0 0 1.83l8.58 3.91a2 2 0 0 0 1.66 0l8.58-3.9a1 1 0 0 0 0-1.83Z', 'M2 12a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 12', 'M2 17a1 1 0 0 0 .58.91l8.6 3.91a2 2 0 0 0 1.65 0l8.58-3.9A1 1 0 0 0 22 17'],
    'list': ['M3 6h.01', 'M3 12h.01', 'M3 18h.01', 'M8 6h13', 'M8 12h13', 'M8 18h13'],
    'list-check': ['M11 6h10', 'M11 12h10', 'M11 18h10', 'm3 6 1.5 1.5L7 5', 'm3 12 1.5 1.5L7 11', 'm3 18 1.5 1.5L7 17'],
    'git': ['<circle cx="6" cy="18" r="3"/>', '<circle cx="18" cy="6" r="3"/>', 'M6 15V6a3 3 0 0 1 3-3', 'M18 9a9 9 0 0 1-9 9'],
    'cloud': ['M17.5 19H9a7 7 0 1 1 6.71-9h1.79a4.5 4.5 0 1 1 0 9Z'],
    'message': ['M7.9 20A9 9 0 1 0 4 16.1L2 22Z'],
    'sun': ['<circle cx="12" cy="12" r="4"/>', 'M12 2v2', 'M12 20v2', 'm4.93 4.93 1.41 1.41', 'm17.66 17.66 1.41 1.41', 'M2 12h2', 'M20 12h2', 'm6.34 17.66-1.41 1.41', 'm19.07 4.93-1.41 1.41'],
    'ban': ['<circle cx="12" cy="12" r="10"/>', 'm4.9 4.9 14.2 14.2'],
    'cpu': ['<rect x="4" y="4" width="16" height="16" rx="2"/>', '<rect x="9" y="9" width="6" height="6"/>', 'M15 2v2', 'M15 20v2', 'M2 15h2', 'M2 9h2', 'M20 15h2', 'M20 9h2', 'M9 2v2', 'M9 20v2'],
    'plug': ['M12 22v-5', 'M9 8V2', 'M15 8V2', 'M18 8v5a6 6 0 0 1-12 0V8Z'],
    'image': ['<rect x="3" y="3" width="18" height="18" rx="2"/>', '<circle cx="9" cy="9" r="2"/>', 'm21 15-3.1-3.1a2 2 0 0 0-2.8 0L6 21'],
    'wifi-off': ['M12 20h.01', 'M8.5 16.4a5 5 0 0 1 7 0', 'M5 12.9a10 10 0 0 1 5.2-2.7', 'M19 12.9a10 10 0 0 0-2.1-1.6', 'M2 8.8a15 15 0 0 1 4.2-2.7', 'M22 8.8a15 15 0 0 0-11.3-3.7', 'm2 2 20 20'],
    'check-circle': ['<circle cx="12" cy="12" r="10"/>', 'm9 12 2 2 4-4'],
    'x-circle': ['<circle cx="12" cy="12" r="10"/>', 'm15 9-6 6', 'm9 9 6 6'],
    'pen': ['M21.2 6.4a2 2 0 0 0 0-2.8l-.8-.8a2 2 0 0 0-2.8 0L4 16.4V20h3.6Z', 'm15 5 4 4'],
    'inbox': ['M22 12h-6l-2 3h-4l-2-3H2', 'M5.45 5.11 2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z'],
}


def tw(s: str, size: float = 13, mono: bool = False) -> float:
    """粗略估算文本宽度：中日韩全角按 1em；ASCII 按字形宽度，等宽字体一律 0.62em。"""
    w = 0.0
    for ch in s:
        o = ord(ch)
        if o < 128:
            if mono:
                w += 0.62
            elif ch in 'iljt.,:;!\'| ':
                w += 0.30
            elif ch in 'mwMW@':
                w += 0.85
            elif ch.isupper() or ch.isdigit():
                w += 0.62
            else:
                w += 0.53
        elif 0x2000 <= o <= 0x206f or 0x2190 <= o <= 0x21ff:
            w += 0.7
        else:
            w += 1.0
    return w * size


def _units(s: str) -> list[str]:
    """折行单位：每个中日韩字符单独一个；连续的 ASCII 非空白字符是一个词；空格跟在前一个单位后面。"""
    units, cur = [], ''
    for ch in s:
        if ord(ch) < 128 and not ch.isspace():
            cur += ch
        else:
            if cur:
                units.append(cur)
                cur = ''
            if ch.isspace():
                if units:
                    units[-1] += ch
                else:
                    units.append(ch)
            else:
                units.append(ch)
    if cur:
        units.append(cur)
    return units


def wrap(s: str, maxw: float, size: float = 13) -> list[str]:
    """按估算宽度折行：不在英文单词中间断开，尽量不让行首出现标点。"""
    maxw -= 4  # 估算偏保守一点，避免顶到卡片边缘
    if tw(s, size) <= maxw:
        return [s]
    lines, cur = [], ''
    for u in _units(s):
        if cur and tw((cur + u).rstrip(), size) > maxw:
            if u in '，。、；：）」』！？' or u.startswith(('，', '。', '、', '；', '：', '）', '」', '』', '！', '？')):
                lines.append((cur + u).rstrip())
                cur = ''
                continue
            lines.append(cur.rstrip())
            cur = u.lstrip()
        else:
            cur += u
    if cur.strip():
        lines.append(cur.rstrip())
    return lines


class Canvas:
    def __init__(self, w: int, h: int, label: str):
        self.w, self.h, self.label = w, h, label
        self.parts: list[str] = []

    # ---- 基础元素
    def text(self, x, y, s, cls='', anchor='start', extra=''):
        a = '' if anchor == 'start' else f' text-anchor="{anchor}"'
        c = f' class="{cls}"' if cls else ''
        self.parts.append(f'<text x="{x:.0f}" y="{y:.0f}"{c}{a}{extra}>{html.escape(str(s))}</text>')

    def lines(self, x, y, items, cls='s', lh=19, anchor='start'):
        for i, s in enumerate(items):
            self.text(x, y + i * lh, s, cls, anchor)
        return y + len(items) * lh

    def para(self, x, y, s, maxw, cls='s', lh=19, size=13, anchor='start'):
        return self.lines(x, y, wrap(s, maxw, size), cls, lh, anchor)

    def rect(self, x, y, w, h, cls='c-gray', r=12, extra=''):
        self.parts.append(f'<rect x="{x:.1f}" y="{y:.1f}" width="{w:.1f}" height="{h:.1f}" rx="{r}" class="{cls}"{extra}/>')

    def circle(self, cx, cy, r, cls):
        self.parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{r}" class="{cls}"/>')

    def path(self, d, cls='line'):
        self.parts.append(f'<path d="{d}" class="{cls}"/>')

    def icon(self, name, x, y, size=22, tone='blue'):
        """把 24×24 的图标画到 (x,y) 为左上角、边长 size 的方框里。"""
        k = size / 24
        inner = ''.join(p if p.startswith('<') else f'<path d="{p}"/>' for p in ICONS[name])
        self.parts.append(f'<g transform="translate({x:.1f},{y:.1f}) scale({k:.3f})" class="ic ic-{tone}">{inner}</g>')

    def arrow(self, pts, tone='', dash=False, label='', lcls='xs', lpos=0.5, ldy=-8):
        """折线箭头。label 放在首段中点：水平段放在线上方，竖直段放在线右侧。"""
        d = 'M' + ' L'.join(f'{x:.1f},{y:.1f}' for x, y in pts)
        cls = 'ar' + (f' {tone}' if tone else '') + (' dash' if dash else '')
        self.path(d, cls)
        if label:
            (x1, y1), (x2, y2) = pts[0], pts[1]
            lx, ly = x1 + (x2 - x1) * lpos, y1 + (y2 - y1) * lpos
            if abs(x2 - x1) >= abs(y2 - y1):
                self.text(lx, ly + ldy, label, lcls, 'middle')
            else:
                self.text(lx + 8, ly + 4, label, lcls, 'start')

    # ---- 组合元素
    def badge(self, cx, cy, n, tone='blue', r=13, size=13):
        self.circle(cx, cy, r, f'f-{tone}')
        self.text(cx, cy + size * 0.36, n, 'w', 'middle', f' style="font-size:{size}px"')

    def chip(self, x, y, s, tone='gray', mono=False, h=24, pad=10, size=12):
        w = tw(s, size, mono) + pad * 2
        self.rect(x, y, w, h, f'c-{tone}', r=h / 2)
        cls = 'mono' if mono else 'xs'
        self.text(x + w / 2, y + h / 2 + size * 0.36, s, f'{cls} t-{tone}', 'middle', f' style="font-size:{size}px"')
        return w

    def pill(self, x, y, s, tone='green', h=26, pad=12, size=13, icon=None):
        iw = 18 if icon else 0
        w = tw(s, size) + pad * 2 + iw
        self.rect(x, y, w, h, f'f-{tone}', r=h / 2)
        if icon:
            self.icon(icon, x + pad - 2, y + (h - 16) / 2, 16, 'white')
        self.text(x + pad + iw + (w - pad * 2 - iw) / 2, y + h / 2 + size * 0.36, s, 'w', 'middle',
                  f' style="font-size:{size}px"')
        return w

    def card(self, x, y, w, h, title='', body=(), tone='gray', number=None, icon=None, icon_tone=None,
             title_cls='h', body_cls='s', pad=16, lh=19, r=12, body_size=13):
        """圆角卡片：可选左上角编号圆、图标；标题一行，正文多行（自动折行）。"""
        self.rect(x, y, w, h, f'c-{tone}', r)
        cx = x + pad
        ty = y + pad + 13
        if number is not None:
            self.badge(x + pad + 12, y + pad + 10, number, icon_tone or (tone if tone != 'gray' else 'blue'))
            cx = x + pad + 34
        elif icon:
            self.icon(icon, x + pad, y + pad - 2, 22, icon_tone or (tone if tone != 'gray' else 'blue'))
            cx = x + pad + 32
        if title:
            self.text(cx, ty + 1, title, title_cls)
            ty += lh + 4
        maxw = x + w - pad - cx
        for line in body:
            for piece in wrap(line, maxw, body_size):
                self.text(cx, ty, piece, body_cls)
                ty += lh
        return ty

    def note(self, x, y, w, text, tone='gray', icon='help', size=13, lh=19, pad=14):
        """一行或多行的提示条，图标在左。"""
        lines = wrap(text, w - pad * 2 - 30, size)
        h = pad * 2 + lh * len(lines) - 4
        self.rect(x, y, w, h, f'c-{tone}', 10)
        self.icon(icon, x + pad, y + pad - 2 + (lh * len(lines) - 22) / 2 - 2, 20, tone if tone != 'gray' else 'blue')
        self.lines(x + pad + 30, y + pad + 10, lines, 's', lh)
        return y + h

    def hline(self, x1, x2, y, cls='line soft'):
        self.path(f'M{x1},{y} L{x2},{y}', cls)

    def render(self) -> str:
        light, dark = _tone_css()
        style = STYLE % {'font': FONT, 'mono': MONO, 'tones': light, 'dtones': dark}
        markers = ''.join(
            f'<marker id="ah{sfx}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="7" markerHeight="7" '
            f'orient="auto-start-reverse"><path d="M1,1 L9,5 L1,9" fill="none" stroke="{col}" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round"/></marker>'
            for sfx, col in [('', '#8c959f'), ('-blue', '#2f5fdb'), ('-green', '#1a7f37'), ('-red', '#cf222e'), ('-amber', '#9a6700')])
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" viewBox="0 0 {self.w} {self.h}" '
                f'role="img" aria-label="{html.escape(self.label)}">\n<style>{style}</style>\n<defs>{markers}</defs>\n'
                f'<rect width="{self.w}" height="{self.h}" class="bg"/>\n' + '\n'.join(self.parts) + '\n</svg>\n')


DIAGRAMS: dict[str, tuple] = {}


def diagram(name, w, h, label):
    def deco(fn):
        DIAGRAMS[name] = (w, h, label, fn)
        return fn
    return deco


def step_card(c: Canvas, x, y, w, h, st, number=None, tone='gray', icon_tone='blue'):
    """步骤卡片：第一行是编号圆和右上角图标，第二行标题，之后是说明。"""
    tone = st.get('tone', tone)
    c.rect(x, y, w, h, f'c-{tone}')
    it = st.get('icon_tone', icon_tone if tone == 'gray' else tone)
    if number is not None:
        c.badge(x + 29, y + 29, number, st.get('badge', it))
    if st.get('icon'):
        c.icon(st['icon'], x + w - 42, y + 18, 24, it)
    ty = y + 70 if number is not None else y + 31
    if number is None and st.get('icon'):
        pass
    c.text(x + 16, ty, st['title'], 'h')
    ty += 24
    for line in st.get('body', ()):
        for piece in wrap(line, w - 32, 13):
            c.text(x + 16, ty, piece, 's')
            ty += 19
    return ty


def step_row(c: Canvas, y, steps, x0=24, gap=18, w=None, h=140, tone='gray', icon_tone='blue', number=True,
             arrow_tone='', start=1):
    """一排步骤卡片，卡片之间用箭头连。number=False 时没有编号圆，标题直接在第一行。"""
    n = len(steps)
    if w is None:
        w = (c.w - x0 * 2 - gap * (n - 1)) / n
    for i, st in enumerate(steps):
        x = x0 + i * (w + gap)
        step_card(c, x, y, w, h, st, (start + i) if number else None, tone, icon_tone)
        if i < n - 1:
            c.arrow([(x + w + 2, y + h / 2), (x + w + gap - 2, y + h / 2)], arrow_tone)
    return y + h


def chip_flow(c: Canvas, x, y, items, gap=26, h=28, size=12.5):
    """一串小胶囊，用短箭头连起来（时间线用）。items: (文字, 色调) 或 (文字, 色调, 图标)。返回末尾 x。"""
    for i, item in enumerate(items):
        s, tone = item[0], item[1]
        icon = item[2] if len(item) > 2 else None
        iw = 18 if icon else 0
        w = tw(s, size) + 22 + iw
        c.rect(x, y, w, h, f'c-{tone}', h / 2)
        if icon:
            c.icon(icon, x + 10, y + (h - 15) / 2, 15, tone if tone != 'gray' else 'blue')
        c.text(x + 11 + iw + (w - 22 - iw) / 2, y + h / 2 + size * 0.36, s, f'xs t-{tone}', 'middle',
               f' style="font-size:{size}px"')
        if i < len(items) - 1:
            c.arrow([(x + w + 3, y + h / 2), (x + w + gap - 3, y + h / 2)])
        x += w + gap
    return x


def chip_wrap(c: Canvas, x, y, maxw, items, tone='gray', mono=False, gap=8, lh=32, size=12):
    """把一组小胶囊按宽度自动换行摆放，返回最后一行的底边 y。"""
    cx, cy = x, y
    for s in items:
        w = tw(s, size, mono) + 20
        if cx > x and cx + w > x + maxw:
            cx, cy = x, cy + lh
        c.chip(cx, cy, s, tone, mono=mono, size=size)
        cx += w + gap
    return cy + 24


# ================================================================ 1. 效果
@diagram('today-tasks', 960, 220, '今日任务：签到领奖已完成，每日答题已完成')
def today_tasks(c: Canvas):
    c.rect(24, 24, 440, 172, 'c-gray', 14)
    c.text(44, 56, '今日任务', 'hb')
    c.hline(44, 444, 72)
    for i, (icon, name) in enumerate([('check-circle', '签到领奖'), ('help', '每日答题')]):
        y = 92 + i * 54
        c.icon(icon, 44, y, 24, 'blue')
        c.text(80, y + 17, name, 'h')
        c.pill(350, y - 2, '已完成', 'green', icon='check')
        if i == 0:
            c.hline(44, 444, y + 40)
    c.rect(496, 24, 440, 104, 'c-blue', 14)
    c.icon('check-circle', 516, 42, 24, 'blue')
    c.text(550, 60, '两项都是「已完成」，今天的目标就达成了', 'h')
    c.lines(516, 90, ['签到领奖 ← 工具替你点了一次签到', '每日答题 ← 工具替你答对了一道题'], 's', 22)
    c.note(496, 144, 440, '这个面板在网站旧版界面的右上角。', 'gray', 'eye')


@diagram('credit-log', 960, 400, '积分流水：每天固定两条 +1')
def credit_log(c: Canvas):
    c.rect(24, 20, 560, 356, 'c-gray', 14)
    c.text(44, 54, '我的积分', 'hb')
    c.text(564, 54, '按网站页面重绘', 'xs', 'end')
    c.hline(44, 564, 72)
    rows = [('每日答题', '昨天'), ('签到奖励', '昨天'), ('每日答题', '2 天前'), ('签到奖励', '2 天前'),
            ('每日答题', '3 天前'), ('签到奖励', '3 天前')]
    for i, (title, when) in enumerate(rows):
        y = 92 + i * 46
        c.chip(44, y, '大米 +1', 'green')
        c.text(140, y + 17, title, 'h')
        c.text(564, y + 17, when, 'xs', 'end')
        if i < len(rows) - 1:
            c.hline(44, 564, y + 36)
    c.rect(616, 20, 320, 168, 'c-blue', 14)
    c.icon('coins', 636, 40, 24, 'blue')
    c.text(670, 58, '一天 = 两条记录', 'h')
    c.lines(636, 92, ['签到奖励　→ 大米 +1', '每日答题　→ 大米 +1'], 's', 24)
    c.text(636, 160, '每天都有这两条，连续签到就不会断。', 'xs')
    c.note(616, 208, 320, '工具每天做完，都会打开这一页，亲眼看到两条「+1」才算成功。', 'gray', 'eye')
    c.note(616, 300, 320, '缺了哪一天，历史命令会把那天单独列出来。', 'gray', 'history')


# ================================================================ 2. 总览
@diagram('overview', 960, 480, '总览：一次自动运行经过的八步')
def overview(c: Canvas):
    row1 = [
        {'title': '到点了，系统叫醒它', 'icon': 'alarm', 'body': ['电脑自带的定时任务，每 10 分钟一次']},
        {'title': '先看看今天做过没', 'icon': 'search', 'body': ['只查本机记录，不开浏览器']},
        {'title': '打开专用 Chrome', 'icon': 'monitor', 'body': ['和你平时用的浏览器互不干扰']},
        {'title': '自动签到', 'icon': 'check', 'body': ['选一个心情，点「提交签到」']},
    ]
    row2 = [
        {'title': '自动答题', 'icon': 'help', 'body': ['查题库，点对应选项，点「提交答案」']},
        {'title': '确认大米真的到账', 'icon': 'coins', 'body': ['打开积分页，看到两条「+1」']},
        {'title': '记在本机', 'icon': 'database', 'body': ['这次做了什么、结果如何']},
        {'title': '报告结果', 'icon': 'file', 'body': ['圆满完成、需要看一眼、或失败']},
    ]
    w, gap, h = 213, 18, 140
    step_row(c, 24, row1, w=w, gap=gap, h=h)
    x2 = 24 + (w + gap) + w / 2
    c.arrow([(x2, 24 + h + 2), (x2, 184)], 'amber', dash=True)
    c.rect(x2 - 118, 186, 236, 30, 'c-amber', 15)
    c.text(x2, 205, '做过了或还没到点 → 安静退出', 'xs t-amber', 'middle')
    x4 = 24 + 3 * (w + gap) + w / 2
    x5 = 24 + w / 2
    c.arrow([(x4, 24 + h + 2), (x4, 228), (x5, 228), (x5, 244)])
    step_row(c, 246, row2, w=w, gap=gap, h=h, start=5)
    c.note(24, 408, 912, '出错了会换一个新的 Chrome 再试一次；之后每 10 分钟还会再来，直到当天两项都确认到账。已经确认到账的，不会重复做。',
           'gray', 'refresh')


@diagram('browser-isolation', 960, 340, '专用 Chrome：和你平时用的浏览器完全隔离')
def browser_isolation(c: Canvas):
    c.card(24, 24, 400, 130, '你平时用的 Chrome', ['你的登录、书签、扩展都在这里', '工具永远不碰它；它跑的时候你照常用'], 'gray', icon='monitor')
    c.path('M480,30 L480,148', 'line soft')
    c.rect(444, 74, 72, 30, 'c-gray', 15)
    c.text(480, 93, '互不干扰', 'xs', 'middle')
    c.card(536, 24, 400, 130, '工具的专用 Chrome', ['独立的配置目录，里面只有论坛的登录', '窗口最小化或隐藏；跑完就关，不常驻'], 'blue', icon='monitor')
    cards = [('lock', '同一时刻只跑一个', '第二个任务会直接退出，不排队'),
             ('hourglass', '一次最多 15 分钟', '超时就从外部关掉，不会一直挂着'),
             ('globe', '只打开论坛的网址', '别的地址在代码里直接拒绝')]
    for i, (ic, t, b) in enumerate(cards):
        c.card(24 + i * 310, 178, 292, 80, t, [b], 'gray', icon=ic)
    c.text(24, 300, '「专用」的意思是：它有自己的一套登录和设置，和你日常浏览器互不可见。', 'xs')


# ================================================================ 3. 签到
@diagram('checkin-flow', 960, 262, '自动签到：五个步骤')
def checkin_flow(c: Canvas):
    steps = [
        {'title': '打开签到页', 'icon': 'globe', 'body': ['没登录的话，先自动登录']},
        {'title': '选一个心情', 'icon': 'smile', 'body': ['默认随机抽一个，配一句说说']},
        {'title': '点「提交签到」', 'icon': 'pointer', 'body': ['只点一次，按钮没就绪就等下一轮']},
        {'title': '等网站回应', 'icon': 'hourglass', 'body': ['最多等 45 秒，等到真实回应才算']},
        {'title': '确认大米到账', 'icon': 'coins', 'body': ['积分页里有「签到奖励 +1」才算成功']},
    ]
    step_row(c, 24, steps, h=140)
    c.note(24, 186, 912, '随机心情默认开启：每天会以你的名义在主页发一句话。不想要，在账号配置里关掉，之后固定选「没心情」、什么都不写。',
           'amber', 'meh')


@diagram('login-flow', 960, 390, '登录是怎么自动恢复的')
def login_flow(c: Canvas):
    c.card(24, 28, 230, 96, '还登录着吗？', ['每次运行开头都先问网站一句'], 'gray', icon='user')
    c.arrow([(256, 76), (318, 76)], 'green', label='还登录着', lcls='xs t-green')
    c.card(320, 28, 250, 96, '核对是不是你的账号', ['网站返回的账号必须和你配置的一致'], 'green', icon='user-check')
    c.arrow([(572, 60), (610, 60)], 'green')
    c.pill(614, 46, '是你的 → 继续签到答题', 'green', icon='check')
    c.arrow([(572, 100), (610, 100)], 'red')
    c.pill(614, 86, '不是 → 立刻停下', 'red', icon='x')
    c.text(616, 132, '绝不用别人的账号操作', 'xs')
    c.arrow([(139, 126), (139, 188)], 'amber', label='过期了', lcls='xs t-amber')
    steps = [
        {'title': '打开网站登录页', 'icon': 'globe', 'body': ['就是网站自己的登录页面']},
        {'title': '填用户名和密码', 'icon': 'key', 'body': ['从系统保管处取出，用完即丢']},
        {'title': '过网站的人机验证', 'icon': 'shield-check', 'body': ['自动等它通过，最多 40 秒']},
        {'title': '登录成功', 'icon': 'check-circle', 'body': ['回到开头，再问一次「还登录着吗」']},
    ]
    step_row(c, 190, steps, h=100, number=False)
    c.arrow([(936, 240), (948, 240), (948, 12), (139, 12), (139, 26)], 'blue', dash=True)
    c.note(24, 316, 912, '登录成功后同样要核对账号。微信扫码是另一条路，适合第一次登录或应急；日常自动恢复只用密码。', 'gray', 'help')


# ================================================================ 4. 答题
@diagram('quiz-flow', 960, 560, '自动答题：读题、查题库、作答、核对到账、学习')
def quiz_flow(c: Canvas):
    row1 = [
        {'title': '读题', 'icon': 'book', 'body': ['直接从网站拿到题目和全部选项，都是文字']},
        {'title': '查题库', 'icon': 'search', 'body': ['仓库自带的题库，加上你电脑上学到的答案']},
        {'title': '点选项', 'icon': 'pointer', 'body': ['只点文字和答案完全一样的那个']},
    ]
    row2 = [
        {'title': '点「提交答案」', 'icon': 'send', 'body': ['只点一次']},
        {'title': '等网站回应', 'icon': 'hourglass', 'body': ['最多等 45 秒']},
        {'title': '确认大米到账', 'icon': 'coins', 'body': ['积分页里有「每日答题 +1」才算成功']},
    ]
    w, gap, h = 292, 18, 140
    step_row(c, 24, row1, w=w, gap=gap, h=h)
    x2 = 24 + (w + gap) + w / 2
    c.arrow([(x2, 24 + h + 2), (x2, 184)], 'amber', dash=True)
    c.rect(x2 - 200, 186, 400, 34, 'c-amber', 17)
    c.icon('alert', x2 - 186, 193, 20, 'amber')
    c.text(x2 + 10, 208, '题库没有这道题？停下来等你补一次答案，不瞎猜', 'xs t-amber', 'middle')
    x3 = 24 + 2 * (w + gap) + w / 2
    x4 = 24 + w / 2
    c.arrow([(x3, 24 + h + 2), (x3, 232), (x4, 232), (x4, 248)])
    step_row(c, 250, row2, w=w, gap=gap, h=h, start=4)
    y3 = 418
    c.text(24, y3, '答完之后学什么：只有「到账」才算证据', 'h')
    cards = [
        ('到账了', ['记住这个答案，下次同题直接用'], 'green', 'check-circle'),
        ('网站说答过，却没到账', ['答错了：记进错误名单，以后不再交同一个答案'], 'red', 'x-circle'),
        ('没等到网站回应', ['什么都不记，不确定的事不当证据'], 'gray', 'help'),
    ]
    for i, (t, b, tone, ic) in enumerate(cards):
        c.card(24 + i * (w + gap), y3 + 14, w, 94, t, b, tone, icon=ic)
    c.text(24, 548, '学到的答案只存在你自己的电脑上，不进仓库、不上传。', 'xs')


@diagram('answer-matching', 960, 360, '题库匹配：按选项文字比，不按 ABCD')
def answer_matching(c: Canvas):
    c.rect(24, 24, 300, 240, 'c-gray')
    c.icon('book', 40, 40, 22, 'blue')
    c.text(72, 57, '网站今天给的题', 'h')
    c.text(40, 88, '美国哪个州没有夏令时？', 's')
    for i, (opt, hit) in enumerate([('夏威夷', False), ('亚利桑那州', True), ('佛罗里达', False), ('内华达', False)]):
        y = 104 + i * 32
        c.rect(40, y, 268, 26, 'c-green' if hit else 'c-gray', 8)
        c.text(54, y + 17, opt, 's t-green' if hit else 's')
        if hit:
            c.icon('check', 282, y + 4, 18, 'green')
    c.text(40, 249, '选项顺序每天可能不一样', 'xs')

    c.rect(352, 24, 256, 240, 'c-gray')
    c.icon('database', 368, 40, 22, 'blue')
    c.text(400, 57, '题库里的答案', 'h')
    c.text(368, 88, '「美国哪个州没有夏令时？」', 's')
    c.chip(368, 102, '亚利桑那州', 'green', size=13, h=28)
    c.lines(368, 156, ['仓库自带的题库（194 题）', '＋ 你电脑上学到的答案', '两边冲突时，以你电脑上的为准'], 'xs', 20)
    c.arrow([(352, 147), (312, 147)], 'green', label='文字完全一样 → 点它', lcls='xs t-green', ldy=-12)

    c.card(636, 24, 300, 240, '这三种情况不算命中', ['· 题库里没有这道题', '· 没有一个选项和答案一样，或有两个都一样', '· 这个答案在你的错误名单里'],
           'amber', icon='alert', lh=22)
    c.pill(652, 214, '停下来等你补答，不瞎猜', 'amber', icon='alert')
    c.note(24, 290, 912, '比对前先统一格式（全角半角、多余空格），然后按选项的文字比，不看 A/B/C/D 的位置，网站把顺序打乱也没关系。', 'gray', 'help')


@diagram('answer-learning', 960, 300, '答完之后学什么：只有到账才算证据')
def answer_learning(c: Canvas):
    c.card(24, 88, 250, 88, '答完了，等网站回应', ['最多 45 秒'], 'gray', icon='hourglass')
    outcomes = [
        ('到账了', '记住这个答案，下次同题直接用', 'green', 'check-circle'),
        ('网站说答过，却没到账', '记为错误答案，以后不再交同一个，不重复扣米', 'red', 'x-circle'),
        ('没等到网站回应', '什么都不记，不确定的事不当证据', 'gray', 'help'),
    ]
    for i, (t, b, tone, ic) in enumerate(outcomes):
        y = 24 + i * 72
        c.card(360, y, 576, 62, t, [b], tone, icon=ic)
        c.arrow([(276, 132), (318, 132), (318, y + 31), (356, y + 31)])
    c.note(24, 246, 912, '学到的答案只存在你自己的电脑上，不进仓库、不上传。万一这个文件坏了，也只是退回仓库题库，签到答题照常。', 'gray', 'book')


# ================================================================ 5. 核对
@diagram('verify-flow', 960, 340, '怎么判断真的成功：四个条件和三种整体结论')
def verify_flow(c: Canvas):
    c.rect(24, 24, 440, 236, 'c-gray')
    c.icon('list-check', 40, 40, 22, 'blue')
    c.text(72, 57, '积分页上的一条记录，要过四关', 'h')
    for i, s in enumerate(['是你自己的账号', '大米确实 +1', '标题是「签到奖励」或「每日答题」', '日期是网站的「今天」（按洛杉矶时间）']):
        y = 84 + i * 30
        c.icon('check-circle', 40, y, 20, 'green')
        c.text(70, y + 15, s, 's')
    c.pill(40, 212, '四关都过 → 这一项才算成功', 'green', icon='check')
    c.text(496, 44, '整天的结论', 'h')
    results = [('圆满完成', '两项都做了，两条奖励都到账', 'green', 'check-circle', 'complete'),
               ('需要看一眼', '有一项没确认：题库没题、点了但没到账……', 'amber', 'alert', 'needs_attention'),
               ('失败', '中途出错：网络、验证、浏览器断开……', 'red', 'x-circle', 'failed')]
    for i, (t, b, tone, ic, code) in enumerate(results):
        y = 58 + i * 68
        c.card(496, y, 440, 60, t, [b], tone, icon=ic)
        c.text(920, y + 29, code, 'mono', 'end')
    c.note(24, 284, 912, '工具不把「点到了按钮」当成功。网站改版、网络或人机验证出问题时，如实报失败。', 'gray', 'eye')


@diagram('status-decision', 960, 450, '一天的结论是怎么算出来的')
def status_decision(c: Canvas):
    qs = [('两项都显示已完成，而且两条奖励都到账了？', '圆满完成', 'green', 'complete'),
          ('有提交发出去了，却一直没等到网站回执？', '失败', 'red', 'failed'),
          ('本机记录说做过了，网站现在却说没做？', '需要看一眼', 'amber', 'needs_attention')]
    for i, (q, a, tone, code) in enumerate(qs):
        y = 24 + i * 94
        c.rect(24, y, 540, 64, 'c-gray')
        c.icon('help', 40, y + 21, 22, 'blue')
        c.text(72, y + 38, q, 'h')
        c.arrow([(566, y + 32), (604, y + 32)], tone, label='是', lcls=f'xs t-{tone}')
        pw = c.pill(608, y + 19, a, tone)
        c.text(608 + pw + 10, y + 37, code, 'mono')
        if i < 2:
            c.arrow([(294, y + 66), (294, y + 116)], '', label='否')
    c.arrow([(294, 278), (294, 304)], '', label='否')
    c.card(24, 306, 540, 64, '需要看一眼', ['比如题库没题、做了但没查到奖励'], 'amber', icon='alert')
    c.text(608, 344, 'needs_attention', 'mono')
    c.note(24, 394, 912, '中途出错也算失败，错误原因原样记下。不论结果如何，这次运行都写进本机记录。', 'gray', 'database')


@diagram('data-files', 960, 300, '结果存在哪：全部在本机')
def data_files(c: Canvas):
    c.rect(24, 24, 912, 196, 'c-gray')
    c.icon('folder', 40, 40, 22, 'blue')
    c.text(72, 57, '你电脑上的 work 文件夹（不进仓库）', 'h')
    files = [('database', '本机数据库', ['每天的运行记录、心跳；用了面经功能才有更多']),
             ('file', '最近一次结果', ['完整原始结果，可能含私人信息，别整段贴出去']),
             ('book', '学到的答案', ['答对到账过的、答错过的']),
             ('lock', '浏览器锁', ['保证同时只有一个任务用浏览器'])]
    w = (912 - 32 - 42) / 4
    for i, (ic, t, b) in enumerate(files):
        c.card(40 + i * (w + 14), 74, w, 128, t, b, 'gray', icon=ic)
    c.note(24, 240, 912, '看历史用历史命令：离线、不开浏览器。同一天跑了多次会合并成一条，并给出健康判定。', 'gray', 'history')


# ================================================================ 6. 重试
@diagram('retry-flow', 960, 560, '失败重试：六层保护')
def retry_flow(c: Canvas):
    layers = [('秒', '读页面遇到网络抖动', '等 1 秒再读一次。只重读，不重复提交'),
              ('秒', '提交后等网站回执', '最多等 45 秒；快超时时自动再试一次人机验证'),
              ('分钟', '一次运行有时限', '最多 15 分钟，超时就从外部关掉 Chrome，记录照样保存'),
              ('分钟', '同一次触发内重来', '网络、验证、浏览器断开这类错误，换一个新 Chrome 再跑一次；做过的不重做'),
              ('小时', '每 10 分钟再来', '没到点、已完成就安静退出；一天还有 6 个恢复时点，重复触发是安全的'),
              ('天', '独立的健康观察者', '只看本机记录，不开浏览器；连续 2 天没完成或 8 小时没心跳，就提醒你')]
    c.path('M52,30 L52,470', 'line soft')
    for i, (scale, t, b) in enumerate(layers):
        y = 24 + i * 76
        c.rect(24, y + 20, 56, 26, 'c-blue', 13)
        c.text(52, y + 37, scale, 'xs t-blue', 'middle')
        c.rect(96, y, 840, 66, 'c-gray')
        c.badge(122, y + 33, i + 1, 'blue')
        c.text(146, y + 28, t, 'h')
        c.text(146, y + 51, b, 's')
    c.note(24, 490, 912, '会自动重试的：网络问题、验证没过、浏览器断开、超时。不盲目重试的：密码错误、题库没题、按钮没点到，这些留给下一轮或由你处理。',
           'gray', 'refresh')


@diagram('retry-timeline', 960, 340, '一个坏日子的接力')
def retry_timeline(c: Canvas):
    lanes = [('同一次触发内', '几分钟内', [('16:10 触发', 'gray', 'alarm'), ('签到 ✓', 'green'), ('答题 ✗ 网络超时', 'red'),
                                       ('换新 Chrome 重跑', 'blue', 'refresh'), ('答题 ✓ 到账 ✓', 'green'), ('圆满', 'green', 'check')]),
             ('下一次计划', '10 分钟后', [('重跑也失败', 'red'), ('16:20 再触发', 'gray', 'alarm'), ('签到已确认，只补答题', 'blue'),
                                     ('答题 ✓', 'green'), ('圆满', 'green', 'check')]),
             ('第二天', '独立的观察者', [('整天都失败', 'red'), ('观察者发现连续没完成', 'amber', 'eye'), ('提醒你', 'amber', 'bell')])]
    for i, (t, sub, items) in enumerate(lanes):
        y = 24 + i * 88
        c.rect(24, y, 170, 64, 'c-gray')
        c.text(40, y + 28, t, 'h')
        c.text(40, y + 50, sub, 'xs')
        chip_flow(c, 214, y + 18, items, gap=22)
    c.note(24, 284, 912, '每一层都不重复已确认的动作：签到到账了，后面只做答题；网站已经回应过的提交也绝不重交。', 'gray', 'shield-check')


@diagram('lost-submission', 960, 410, '提交发出去了回执却没回来')
def lost_submission(c: Canvas):
    steps = [{'title': '点了「提交」', 'icon': 'send', 'body': ['签到或答题']},
             {'title': '等网站回执', 'icon': 'hourglass', 'body': ['最多 45 秒']},
             {'title': '没等到', 'icon': 'alert', 'body': ['记为「回执没回来」，这次先算失败'], 'tone': 'amber'}]
    step_row(c, 24, steps, w=292, gap=18, h=84, number=False)
    c.arrow([(790, 110), (790, 136)])
    c.rect(24, 138, 912, 34, 'c-blue', 17)
    c.text(480, 160, '下一次运行（10 分钟后）：先看网站怎么说，再决定要不要再提交', 'xs t-blue', 'middle')
    outcomes = [('网站说已完成或已到账', ['上次其实成功了 → 不再提交，只补核对'], 'green', 'check-circle'),
                ('网站说没完成，且从没回应过', ['结果未知 → 保留记录，只核验不重交'], 'blue', 'refresh'),
                ('网站说没完成，但回应过一次', ['网站处理过了 → 绝不重交，只查回执，等人看'], 'red', 'x-circle')]
    for i, (t, b, tone, ic) in enumerate(outcomes):
        x = 24 + i * 310
        c.arrow([(x + 146, 174), (x + 146, 194)])
        c.card(x, 196, 292, 116, t, b, tone, icon=ic)
    c.note(24, 334, 912, '点击前先保存意图。即使进程中断，恢复时也先核验；只有明确未点击才允许再次提交。',
           'gray', 'help')


@diagram('health-watch', 960, 300, '健康观察者：第二双眼睛')
def health_watch(c: Canvas):
    c.card(24, 24, 330, 150, '主计划：每 10 分钟一次', ['该跑就跑，不该跑就退出；每次都在本机记一次心跳',
                                              '盲点：定时任务坏了、电脑没开时，它根本不会被叫醒，也就不会报错'], 'gray', icon='alarm')
    c.rect(400, 64, 160, 70, 'c-gray')
    c.icon('database', 468, 76, 24, 'blue')
    c.text(480, 122, '本机记录', 'h', 'middle')
    c.arrow([(356, 99), (398, 99)], label='写')
    c.arrow([(604, 99), (562, 99)], label='读')
    c.card(606, 24, 330, 150, '健康观察者：每天一次', ['只读本机记录，不开浏览器、不碰账号',
                                            '看两件事：该完成的日子都完成了吗？最近一次心跳是多久前？'], 'blue', icon='eye')
    verdicts = [('正常', '都完成了，8 小时内有心跳', 'green', 'check-circle'),
                ('留意', '有一天没完成，但还不到 2 天', 'amber', 'alert'),
                ('提醒你', '连续 2 天没完成，或 8 小时没心跳', 'red', 'bell')]
    for i, (t, b, tone, ic) in enumerate(verdicts):
        c.card(24 + i * 310, 198, 292, 62, t, [b], tone, icon=ic)
    c.text(24, 288, '「提醒你」时命令会返回一个特殊的退出码，你可以接桌面通知或消息推送。', 'xs')


# ================================================================ 7. 调度
def _axis(c: Canvas, x0, x1, y, labels, label_y, minor=True):
    c.path(f'M{x0},{y} L{x1},{y}', 'line')
    span = x1 - x0
    for i, s in enumerate(labels):
        x = x0 + span * i / (len(labels) - 1)
        c.path(f'M{x:.1f},{y - 6} L{x:.1f},{y + 6}', 'line')
        c.text(x, label_y, s, 'xs', 'middle')
    if minor:
        for k in range(1, 144):
            x = x0 + span * k / 144
            c.path(f'M{x:.1f},{y} L{x:.1f},{y + 3}', 'line soft')


@diagram('schedule-flow', 960, 300, '调度：一天的时间线')
def schedule_flow(c: Canvas):
    x0, x1, y = 64, 896, 124
    c.text(24, 40, '一天的时间线（默认计划时间 16:10，按北京时间；时间和时区都可以改）', 'h')
    _axis(c, x0, x1, y, ['00:00', '04:00', '08:00', '12:00', '16:00', '20:00', '24:00'], 150)
    span = x1 - x0
    for hour in range(0, 24, 4):
        x = x0 + span * (hour + 10 / 60) / 24
        c.path(f'M{x:.1f},{y - 7} L{x + 6:.1f},{y} L{x:.1f},{y + 7} L{x - 6:.1f},{y} Z', 'f-amber')
    px = x0 + span * (16 + 10 / 60) / 24
    c.path(f'M{px:.1f},{y - 44} L{px:.1f},{y + 8}', 'ar blue')
    c.text(px, y - 52, '计划时间 16:10', 'h t-blue', 'middle')
    c.text(x0 + (px - x0) / 2, 178, '没到点：直接退出，不开浏览器', 'xs', 'middle')
    c.text(px + (x1 - px) / 2, 178, '到点后执行；做完就是「今天已完成」', 'xs', 'middle')
    cx = 24
    cx += c.chip(cx, 200, '小刻度 = 每 10 分钟醒来看一眼', 'gray') + 10
    cx += c.chip(cx, 200, '◆ 恢复时点：每 4 小时一个，给按固定时间触发的定时任务用', 'amber') + 10
    c.note(24, 238, 912, '电脑睡着错过的，醒来后下一次补上，只补今天不补过去。每次醒来都记一次心跳。不要给同一个账号配两个定时任务。', 'gray', 'moon')


@diagram('site-day', 960, 342, '网站的一天按洛杉矶时间算')
def site_day(c: Canvas):
    x0, x1 = 64, 896
    span = x1 - x0
    c.text(24, 40, '北京时间（你电脑的时钟，假设在国内）', 'h')
    _axis(c, x0, x1, 96, ['00:00', '04:00', '08:00', '12:00', '16:00', '20:00', '24:00'], 82, minor=False)
    bx = x0 + span * 15 / 24
    c.rect(x0, 108, bx - x0, 30, 'c-gray', 8)
    c.text(x0 + (bx - x0) / 2, 127, '网站日：昨天', 'xs', 'middle')
    c.rect(bx, 108, x1 - bx, 30, 'c-blue', 8)
    c.text(bx + (x1 - bx) / 2, 127, '网站日：今天', 'xs t-blue', 'middle')
    c.path(f'M{bx:.1f},60 L{bx:.1f},215', 'ar amber dash')
    c.text(bx - 8, 60, '网站的新一天从这里开始', 'xs t-amber', 'end')
    c.text(24, 252, '洛杉矶时间（网站算「一天」的依据）', 'h')
    _axis(c, x0, x1, 206, ['09:00', '13:00', '17:00', '21:00', '01:00', '05:00', '09:00'], 226, minor=False)
    px = x0 + span * (16 + 10 / 60) / 24
    c.path(f'M{px:.1f},160 L{px:.1f},138', 'ar blue')
    c.text(px + 8, 172, '计划 16:10：刚翻到「今天」就签，留足一整天补救', 'xs t-blue')
    c.text(24, 274, '洛杉矶 00:00 = 北京 15:00（夏令时）或 16:00（冬令时）', 'xs')
    c.note(24, 290, 912, '奖励记录的时间也折算成洛杉矶日期再比较。你在美国、在国内、出差、夏令时切换，判断都一致，因为从不看电脑的本地日期。',
           'gray', 'globe')


# ================================================================ 8. 安全
@diagram('security', 960, 420, '安全与隐私')
def security(c: Canvas):
    c.rect(24, 24, 444, 270, 'c-gray')
    c.icon('globe', 40, 40, 22, 'blue')
    c.text(72, 57, '仓库里：公开，任何人可见', 'h')
    chip_wrap(c, 40, 80, 412, ['源码', '题库（194 题）', '心情短句', '配置样例（不含真实信息）', '测试', '文档和配图'], 'gray')
    c.para(40, 196, '仓库默认忽略一切文件，只有登记过的才进得去；检查命令还会扫描密钥特征和个人路径，发现就失败。', 412, 'xs', 18, 12)
    c.rect(492, 24, 444, 270, 'c-blue')
    c.icon('lock', 508, 40, 22, 'blue')
    c.text(540, 57, '只在你电脑上：永不入库', 'h')
    chip_wrap(c, 508, 80, 412, ['账号配置（用户名 + uid）', '密码（交给系统保管）', '浏览器的登录状态', '本机数据库', '最近一次结果', '学到的答案', 'Python 环境'], 'blue')
    c.para(508, 212, '密码在 macOS 放进钥匙串、在 Windows 用系统加密，和这台电脑绑定；换电脑要重新配，拷文件过去没用。', 412, 'xs', 18, 12)
    c.card(24, 318, 912, 78, '网络上只跟论坛自己的网址说话', ['不经过任何第三方服务器、不用打码平台、不上传截图。别的网址在代码里直接拒绝。'], 'green', icon='shield-check')


@diagram('password-flow', 960, 290, '账号和密码是怎么配置、怎么保存的')
def password_flow(c: Canvas):
    steps = [{'title': '写账号文件', 'icon': 'file', 'body': ['只有用户名和 uid，不敏感']},
             {'title': '输一次密码', 'icon': 'key', 'body': ['在终端里隐藏输入，屏幕上不显示']},
             {'title': '交给系统保管', 'icon': 'lock', 'body': ['macOS 放进钥匙串，Windows 用系统加密，只有这台电脑、这个用户能解开']},
             {'title': '需要时才取出', 'icon': 'shield', 'body': ['只在重新登录那一刻取出，填进登录页就丢掉']}]
    step_row(c, 24, steps, h=160)
    c.note(24, 208, 912, '密码不写日志、不进 AI 助手的对话、不进仓库。换电脑要重新配；改了论坛密码就重做第 2、3 步。', 'gray', 'shield-check')


# ================================================================ 9. 快速开始
@diagram('install-flow', 960, 430, '安装七步')
def install_flow(c: Canvas):
    row1 = [{'title': '克隆仓库', 'icon': 'download', 'body': ['拿到源码；不会创建任何计划']},
            {'title': '建环境、装依赖', 'icon': 'cpu', 'body': ['独立的 Python 环境，不污染系统']},
            {'title': '跑一次安装检查', 'icon': 'list-check', 'body': ['不登录、不签到；看到「完成」再继续']},
            {'title': '配账号、存密码', 'icon': 'key', 'body': ['用户名和 uid 写文件，密码交给系统保管']}]
    row2 = [{'title': '登录一次', 'icon': 'user-check', 'body': ['用保管的密码建立登录']},
            {'title': '手动跑一次', 'icon': 'play', 'body': ['看到「圆满完成」；题库没题就补一次']},
            {'title': '配每日计划', 'icon': 'alarm', 'body': ['每 10 分钟一次，从此不用管']}]
    w, gap, h = 213, 18, 140
    step_row(c, 24, row1, w=w, gap=gap, h=h)
    x4 = 24 + 3 * (w + gap) + w / 2
    x5 = 24 + w / 2
    c.arrow([(x4, 24 + h + 2), (x4, 186), (x5, 186), (x5, 202)])
    step_row(c, 204, row2, w=w, gap=gap, h=h, start=5)
    c.card(24 + 3 * (w + gap), 204, w, h, '或者：交给 AI 助手', ['这七步它替你做；密码在它弹出的安全输入框里输，助手拿不到明文'], 'blue', icon='bot')
    c.note(24, 366, 912, '每台电脑做一次，之后全自动。每一步的具体命令见使用说明。', 'gray', 'help')


# ================================================================ 10–12. 给开发者
def _bands(c: Canvas, y, bands, label_w=150, mono=False, gap=10):
    for label, chips, tone in bands:
        rows = []
        cx, maxw = 0, 912 - label_w - 32
        for s in chips:
            w = tw(s, 12, mono) + 20
            if cx and cx + w > maxw:
                rows.append(1)
                cx = 0
            cx += w + 8
        n = len(rows) + 1
        h = 30 + n * 32 - 8
        c.rect(24, y, 912, h, f'c-{tone}')
        c.text(40, y + h / 2 + 5, label, 'h')
        chip_wrap(c, 24 + label_w, y + 15, maxw, chips, tone if tone != 'gray' else 'gray', mono=mono)
        y += h + gap
    return y


@diagram('tech-stack', 960, 440, '技术栈一张图')
def tech_stack(c: Canvas):
    y = _bands(c, 24, [
        ('入口', ['命令行：运行.sh / 运行.cmd', 'MCP 服务：mcp_server.py（给 Claude Code、Codex 用）'], 'gray'),
        ('应用', ['Python 3.12', '15 个模块', '第三方库只有 8 个', 'SeleniumBase + mycdp', 'requests', 'beautifulsoup4', 'mcp + pydantic',
                'rapidocr-onnxruntime', 'tzdata'], 'gray'),
        ('浏览器', ['本机安装的 Chrome', '专用配置目录', 'CDP 协议驱动', '新版接口 tRPC · 老版页面 Discuz'], 'gray'),
        ('存储与凭据', ['SQLite 数据库', '几个 JSON 文件', 'macOS 钥匙串', 'Windows DPAPI', '全部在 work/ 里'], 'gray'),
        ('系统与质量', ['macOS launchd / Windows 任务计划程序', 'unittest：35 个文件、419 个用例', 'GitHub Actions：Ubuntu · Windows · macOS',
                   'architecture.json 白名单 + AST 扫描'], 'gray'),
    ])
    c.text(24, y + 14, '依赖版本全部锁死在 requirements.txt，检查命令会核对已安装版本和清单一致。', 'xs')


@diagram('architecture', 960, 470, '代码架构：五层模块')
def architecture(c: Canvas):
    c.text(24, 40, '五层，依赖只能往下指', 'h')
    c.text(936, 40, '↓ 上层可以用下层，下层不知道上层', 'xs', 'end')
    y = _bands(c, 54, [
        ('入口层', ['cli.py 命令行', 'mcp_server.py MCP 服务', 'check.py · governance.py · repository_checks.py 离线门禁'], 'gray'),
        ('业务层', ['daily.py 每日签到答题', 'interact.py 发帖回复', 'library.py 本机数据库'], 'gray'),
        ('站点层', ['browser.py 唯一联网的模块：专用 Chrome、登录、人机验证、看门狗'], 'blue'),
        ('支撑层', ['rules.py 纯规则', 'secure.py 钥匙串 / DPAPI', 'extract.py 解析页面', 'presentation.py 展示文案'], 'gray'),
        ('基础层', ['settings.py 配置', 'contracts.py 状态与契约', 'architecture.json 白名单'], 'gray'),
    ], mono=True)
    c.note(24, y + 6, 912, '哪个模块能引用哪个，都写在 architecture.json 里；检查命令扫一遍源码验证，越界就失败。', 'gray', 'shield-check')


@diagram('repo-layout', 960, 380, '仓库长什么样')
def repo_layout(c: Canvas):
    rows = [(0, 'folder', '1point3acres-toolkit/', '仓库根目录'),
            (1, 'file', 'README.md', '本文：原理讲解'),
            (1, 'folder', 'docs/', '本文的配图，以及重画它们的脚本'),
            (1, 'folder', '.github/', 'CI 流水线'),
            (1, 'folder', 'outputs/一亩三分地本地工具/', '全部代码都在这一个目录里'),
            (2, 'list', '15 个 Python 模块 · 题库 · 心情短句 · 依赖清单', ''),
            (2, 'list', 'tests/ 35 个测试文件 · 离线阅读器 · 入口脚本 · 使用说明', ''),
            (1, 'folder', 'work/', '不入库：每台电脑自己的 Python 环境、账号、数据库、专用 Chrome')]
    c.rect(24, 24, 912, 340, 'c-gray')
    for i, (depth, ic, name, desc) in enumerate(rows):
        y = 48 + i * 40
        x = 48 + depth * 30
        if depth:
            c.path(f'M{x - 16},{y - 20} L{x - 16},{y + 12} L{x - 4},{y + 12}', 'line soft')
        c.icon(ic, x, y, 20, 'blue' if ic == 'folder' else 'gray')
        c.text(x + 30, y + 15, name, 'mono' if ic != 'list' else 's', extra=' style="font-size:13px"')
        if desc:
            c.text(x + 30 + tw(name, 13, ic != 'list') + 16, y + 15, desc, 'xs')


@diagram('ci-flow', 960, 270, 'CI 流水线')
def ci_flow(c: Canvas):
    steps = [{'title': '静态检查', 'icon': 'list-check', 'body': ['Ubuntu，几秒钟', '依赖白名单、文件清单、文档链接、密钥扫描']},
             {'title': '单元测试', 'icon': 'cpu', 'body': ['Windows 2022 / 2025、macOS 15', '419 个用例，含真实钥匙串往返']},
             {'title': '集成测试', 'icon': 'monitor', 'body': ['Windows 2025、macOS 15', '真实起 MCP 服务、真实开 Chrome；不登录不签到']},
             {'title': '汇总', 'icon': 'check-circle', 'body': ['三个阶段全绿才算通过']}]
    step_row(c, 24, steps, h=158)
    c.note(24, 204, 912, '本地跑检查命令，和 CI 跑的是同一个程序、同样的输出。每个阶段都先重建生成文件再比对，不一致也算失败。', 'gray', 'refresh')


@diagram('commands-map', 960, 450, '命令一览：按用途分组')
def commands_map(c: Canvas):
    groups = [('alarm', '每日任务', '只动自己账号的签到答题', ['daily', 'daily --resume', 'status', 'daily-history', 'save-credentials']),
              ('user', '会话', '只管工具自己的专用 Chrome', ['session-status', 'session-login', '--method wechat', 'session-logout']),
              ('eye', '只读浏览', '不入库、不标记已读', ['site-search', 'thread-detail', 'browse-board', 'user-profile', 'my-profile', 'unread', 'notifications']),
              ('book', '面经资料', '存进本机数据库，可离线搜索、导出', ['collect', 'search', 'export', 'save-thread', 'organize', 'archive-media', 'recognize-media', 'task-*']),
              ('message', '互动', '默认只预览，加 --submit 才真写', ['post', 'reply', 'favorite', 'like', 'reply-notification', 'like-notification']),
              ('list-check', '检查', '不碰账号，和 CI 跑同一个程序', ['检查.sh / 检查.cmd', '--sync', '--stage static | unit | integration'])]
    for i, (ic, t, sub, chips) in enumerate(groups):
        x, y = 24 + (i % 3) * 310, 24 + (i // 3) * 206
        c.rect(x, y, 292, 190, 'c-gray')
        c.icon(ic, x + 16, y + 16, 22, 'blue')
        c.text(x + 48, y + 33, t, 'h')
        c.text(x + 16, y + 58, sub, 'xs')
        chip_wrap(c, x + 16, y + 72, 260, chips, 'gray', mono=True)
    c.text(24, 440, '所有命令都在工具目录里执行，输出都是 JSON。', 'xs')


@diagram('mcp-flow', 960, 250, '接给 AI 助手：MCP 是怎么接的')
def mcp_flow(c: Canvas):
    cards = [('bot', 'AI 助手', ['Claude Code、Codex，或任何支持本地 MCP 的客户端']),
             ('plug', '工具的 MCP 服务', ['32 个工具，每个都标明只读还是可写；多传的参数直接拒绝']),
             ('layers', '同一套函数', ['和命令行完全相同：同一份登录、同一个数据库、同样的安全边界'])]
    for i, (ic, t, b) in enumerate(cards):
        c.card(24 + i * 326, 24, 260, 120, t, b, 'blue' if i == 1 else 'gray', icon=ic)
    c.arrow([(286, 84), (346, 84)], label='本机管道', ldy=-12)
    c.arrow([(612, 84), (672, 84)], label='直接调用', ldy=-12)
    c.note(24, 166, 912, '全程在本机进程之间通信，不开任何网络端口。会真的操作网站的工具（比如跑每日任务），让助手先确认再调；纯离线的可以随便试。', 'gray', 'help')


@diagram('troubleshooting-map', 960, 470, '看到一个错误码，先分清是哪一类')
def troubleshooting_map(c: Canvas):
    cols = [('green', 'check-circle', '不是故障', '什么都不用做',
             [('not_due', '还没到你设的时间'), ('already_complete', '今天已经确认完成'), ('already_done', '这一项早就做过了')]),
            ('amber', 'wrench', '要你动手一次', '改好就恢复',
             [('answer_needed', '题库没题，补一次答案'), ('account_not_configured', '还没写账号文件'),
              ('login_required_\ncredentials_not_configured', '还没存密码'), ('login_rejected', '密码不对，别连续重试'),
              ('unexpected_account', '登的不是你的账号')]),
            ('blue', 'refresh', '等下一轮', '通常自动恢复',
             [('button_not_ready', '页面没就绪'), ('page_challenge_not_resolved', '人机验证没过'), ('network_timeout', '网络抖了一下'),
              ('browser_connection_lost', '多半是电脑睡着了'), ('daily_run_timeout', '超过 15 分钟被结束')]),
            ('gray', 'laptop', '环境问题', '检查电脑上的东西',
             [('another_task_is_\nusing_the_browser', '别开两个，等上一个结束'), ('chrome_profile_busy_\nor_start_failed', 'Chrome 装了吗，路径对吗'),
              ('consistency_check_failed', '跑检查命令，按提示修'), ('daily_history_unavailable', '本机数据库打不开')])]
    for i, (tone, ic, t, sub, items) in enumerate(cols):
        x = 24 + i * 231
        c.rect(x, 24, 213, 350, f'c-{tone}')
        c.icon(ic, x + 16, 40, 22, tone if tone != 'gray' else 'blue')
        c.text(x + 48, 57, t, 'h')
        c.text(x + 16, 80, sub, 'xs')
        y = 108
        for code, desc in items:
            for line in code.split('\n'):
                c.text(x + 16, y, line, 'mono', extra=' style="font-size:11.5px"')
                y += 15
            c.text(x + 16, y + 3, desc, 'xs')
            y += 30
    c.note(24, 396, 912, '提问题时给出系统、Python、Chrome 版本和用到的命令；不要贴账号配置或完整的结果文件。', 'gray', 'help')


@diagram('update-flow', 960, 250, '更新到新版本：五步')
def update_flow(c: Canvas):
    steps = [{'title': '暂停每日计划', 'icon': 'pause', 'body': ['别让它在更新到一半时跑起来']},
             {'title': '确认没有本地改动', 'icon': 'git', 'body': ['有自己的改动先处理掉']},
             {'title': '拉取新代码', 'icon': 'download', 'body': ['只接受顺利合并，有冲突就停下来看']},
             {'title': '重装依赖、跑检查', 'icon': 'list-check', 'body': ['依赖版本可能变了，检查通过才继续']},
             {'title': '恢复计划', 'icon': 'play', 'body': ['之后第一次触发照常判断该不该跑']}]
    step_row(c, 24, steps, h=140)
    c.note(24, 186, 912, 'work 文件夹里的账号、密码、数据库、Chrome 配置都不受更新影响。', 'gray', 'shield-check')


# ================================================================ 主程序
def main(argv):
    names = argv or list(DIAGRAMS)
    for name in names:
        w, h, label, fn = DIAGRAMS[name]
        c = Canvas(w, h, label)
        fn(c)
        (OUT / f'{name}.svg').write_text(c.render(), encoding='utf-8')
        print(f'{name}.svg  {w}×{h}')


if __name__ == '__main__':
    main(sys.argv[1:])
