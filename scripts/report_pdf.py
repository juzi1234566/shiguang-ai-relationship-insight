# -*- coding: utf-8 -*-
"""通用 Markdown → PDF 渲染器(暖色调，微软雅黑)。复用自 gen_growth_report_pdf 的样式。

支持的 Markdown 子集：## / ### 标题、段落、> 引用块、--- 分隔线、
- 无序列表、| 管道表格、行内 **粗体**。

可编程调用：
    from report_pdf import render
    render(md_text, out_pdf, title="标题", subtitle="副标题",
           date_range="2025.09 — 2026.07", dedication="—— 献给…", footer="页脚")
"""
import os
import re

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (Flowable, HRFlowable, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)

_FONTS_READY = False


def _fonts():
    global _FONTS_READY
    if _FONTS_READY:
        return
    pdfmetrics.registerFont(TTFont("YH", r"C:\Windows\Fonts\msyh.ttc", subfontIndex=0))
    pdfmetrics.registerFont(TTFont("YHB", r"C:\Windows\Fonts\msyhbd.ttc", subfontIndex=0))
    pdfmetrics.registerFont(TTFont("YHL", r"C:\Windows\Fonts\msyhl.ttc", subfontIndex=0))
    _FONTS_READY = True


ACCENT = colors.HexColor("#A8443B")
INK = colors.HexColor("#2B2825")
MUTED = colors.HexColor("#8A7F76")
CREAM = colors.HexColor("#F6EFE8")
LINE = colors.HexColor("#E3D8CC")


def _styles():
    return {
        "body": ParagraphStyle("body", fontName="YH", fontSize=10.5, leading=19,
                               textColor=INK, spaceAfter=8, alignment=TA_LEFT),
        "h2": ParagraphStyle("h2", fontName="YHB", fontSize=16, leading=24,
                             textColor=ACCENT, spaceBefore=16, spaceAfter=8),
        "h3": ParagraphStyle("h3", fontName="YHB", fontSize=12, leading=20,
                             textColor=INK, spaceBefore=8, spaceAfter=4),
        "quote": ParagraphStyle("quote", fontName="YH", fontSize=10.5, leading=19,
                               textColor=colors.HexColor("#5B4A3E")),
        "bullet": ParagraphStyle("bullet", fontName="YH", fontSize=10.5, leading=19,
                                textColor=INK, leftIndent=12, bulletIndent=2, spaceAfter=4),
        "cell": ParagraphStyle("cell", fontName="YH", fontSize=9, leading=14, textColor=INK),
        "cellh": ParagraphStyle("cellh", fontName="YHB", fontSize=9.5, leading=14,
                                textColor=colors.white),
    }


def inline(t):
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    return t


class QuoteBox(Flowable):
    def __init__(self, text, width, st):
        super().__init__()
        self.width = width
        self.para = Paragraph(inline(text).replace("\n", "<br/>"), st["quote"])

    def wrap(self, availWidth, availHeight):
        self.width = availWidth
        _, ph = self.para.wrap(availWidth - 30, availHeight)
        self.height = ph + 16
        return availWidth, self.height

    def draw(self):
        c = self.canv
        c.setFillColor(CREAM)
        c.roundRect(0, 0, self.width, self.height, 3, stroke=0, fill=1)
        c.setFillColor(ACCENT)
        c.rect(0, 0, 3.5, self.height, stroke=0, fill=1)
        self.para.drawOn(c, 18, 8)


def _cover(title, subtitle, date_range, dedication, st):
    els = [Spacer(1, 66 * mm)]
    els.append(Paragraph(title, ParagraphStyle(
        "ct", fontName="YHB", fontSize=36, leading=44, textColor=ACCENT, alignment=TA_CENTER)))
    if subtitle:
        els.append(Spacer(1, 6 * mm))
        els.append(Paragraph(subtitle, ParagraphStyle(
            "cs", fontName="YH", fontSize=15, leading=22, textColor=INK, alignment=TA_CENTER)))
    if date_range:
        els.append(Spacer(1, 4 * mm))
        els.append(Paragraph(date_range, ParagraphStyle(
            "cd", fontName="YHL", fontSize=11, leading=18, textColor=MUTED, alignment=TA_CENTER)))
    els.append(Spacer(1, 10 * mm))
    els.append(HRFlowable(width="34%", thickness=1, color=LINE, hAlign="CENTER"))
    if dedication:
        els.append(Spacer(1, 10 * mm))
        els.append(Paragraph(dedication, ParagraphStyle(
            "cf", fontName="YHL", fontSize=10.5, leading=16, textColor=MUTED, alignment=TA_CENTER)))
    els.append(PageBreak())
    return els


def _parse(md, content_width, st):
    lines = md.split("\n")
    els, i = [], 0
    while i < len(lines):
        ln = lines[i].rstrip()
        if not ln.strip():
            i += 1
            continue
        if ln.strip() == "---":
            els += [Spacer(1, 4), HRFlowable(width="100%", thickness=0.8, color=LINE), Spacer(1, 6)]
            i += 1
            continue
        if ln.startswith("# ") and not ln.startswith("## "):
            els.append(Paragraph(inline(ln[2:]), st["h2"]))
            els.append(HRFlowable(width="100%", thickness=0.8, color=ACCENT, spaceAfter=6))
            i += 1
            continue
        if ln.startswith("### "):
            els.append(Paragraph(inline(ln[4:]), st["h3"]))
            i += 1
            continue
        if ln.startswith("## "):
            els.append(Paragraph(inline(ln[3:]), st["h2"]))
            els.append(HRFlowable(width="100%", thickness=0.8, color=ACCENT, spaceAfter=6))
            i += 1
            continue
        if ln.startswith(">"):
            buf = []
            while i < len(lines) and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].strip())
                i += 1
            els += [Spacer(1, 2), QuoteBox("\n".join(buf), content_width, st), Spacer(1, 6)]
            continue
        if ln.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            header = rows[0]
            data = rows[2:]
            tbl = [[Paragraph(inline(c), st["cellh"]) for c in header]]
            for r in data:
                tbl.append([Paragraph(inline(c), st["cell"]) for c in r])
            ncol = len(header)
            widths = ([content_width * w for w in (0.16, 0.46, 0.38)]
                      if ncol == 3 else [content_width / ncol] * ncol)
            t = Table(tbl, colWidths=widths, repeatRows=1)
            t.setStyle(TableStyle([
                ("BACKGROUND", (0, 0), (-1, 0), ACCENT),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, CREAM]),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ("LEFTPADDING", (0, 0), (-1, -1), 7), ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                ("LINEBELOW", (0, 0), (-1, -1), 0.4, LINE),
            ]))
            els += [Spacer(1, 4), t, Spacer(1, 8)]
            continue
        if ln.lstrip().startswith("- "):
            els.append(Paragraph(inline(ln.lstrip()[2:]), st["bullet"], bulletText="•"))
            i += 1
            continue
        els.append(Paragraph(inline(ln), st["body"]))
        i += 1
    return els


def render(md_text, out_pdf, title="报告", subtitle="", date_range="",
           dedication="", footer="报告"):
    _fonts()
    st = _styles()

    def _footer(canvas, doc):
        canvas.saveState()
        canvas.setFont("YHL", 8)
        canvas.setFillColor(MUTED)
        if doc.page > 1:
            canvas.drawCentredString(A4[0] / 2, 12 * mm, f"· {doc.page - 1} ·")
            canvas.drawString(20 * mm, 12 * mm, footer)
        canvas.restoreState()

    doc = SimpleDocTemplate(
        out_pdf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=20 * mm, bottomMargin=20 * mm, title=title)
    cw = A4[0] - 40 * mm
    story = _cover(title, subtitle, date_range, dedication, st) + _parse(md_text, cw, st)
    doc.build(story, onLaterPages=_footer)
    return out_pdf


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--md", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="报告")
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--date-range", default="")
    ap.add_argument("--dedication", default="")
    ap.add_argument("--footer", default="报告")
    a = ap.parse_args()
    render(open(a.md, encoding="utf-8").read(), a.out, a.title, a.subtitle,
           a.date_range, a.dedication, a.footer)
    print(f"[+] {a.out}  {os.path.getsize(a.out)/1024:.0f} KB")
