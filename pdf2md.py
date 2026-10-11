#!/usr/bin/env python3
"""
pdf2md — 將 PDF 轉換為 Markdown（以 PyMuPDF 為基礎）

功能：
  * 依字級大小自動判斷標題層級（#～######），單獨一行的粗體短句也視為小標題
  * 保留粗體、斜體與超連結；等寬字型轉為行內程式碼或程式碼區塊
  * 辨識項目符號與編號清單（含巢狀層級）
  * 偵測有框線的表格並轉為 Markdown 表格
  * 擷取圖片另存檔案，並以相對路徑嵌入
  * 自動移除每頁重複的頁首、頁尾與頁碼
  * 合併被換行切斷的段落（含跨頁）：英文處理連字號斷字，中日文換行不補空白

用法：
  python pdf2md.py 報告.pdf                     產生同名的 報告.md
  python pdf2md.py 報告.pdf -o 輸出/報告.md
  python pdf2md.py 資料夾 -o 輸出資料夾 -r       批次轉換（含子資料夾）
  python pdf2md.py 報告.pdf --pages 1-5,8 --no-images
"""

from __future__ import annotations

import argparse
import os
import re
import statistics
import sys
from collections import Counter
from dataclasses import dataclass, field, replace
from itertools import groupby
from pathlib import Path

try:
    import pymupdf
except ImportError:  # 舊版 PyMuPDF 只提供 fitz 這個名稱
    try:
        import fitz as pymupdf
    except ImportError:
        sys.exit("找不到 PyMuPDF，請先執行：pip install pymupdf")

if hasattr(pymupdf, "no_recommend_layout"):  # 關閉新版 PyMuPDF 推薦安裝 pymupdf_layout 的訊息
    pymupdf.no_recommend_layout()


# ---------------------------------------------------------------------------
# 常數與正規表示式
# ---------------------------------------------------------------------------

# 中日文字元與全形標點：換行合併時不補空白（韓文以空白分詞，不列入）
_CJK = (
    "⺀-⿟　-〿぀-ヿ㄀-ㄯㆠ-ㇿ"
    "㐀-䶿一-鿿豈-﫿︰-﹏＀-￯"
)
CJK_RE = re.compile(f"[{_CJK}]")
LETTER_RE = re.compile(r"[^\W\d_]")

BULLET_CHARS = "•●○◦▪▫■□◆◇►▸▹▶➢➤✓✔❖⁃∙·-–*+"
BULLET_RE = re.compile(rf"\s*([{re.escape(BULLET_CHARS)}])(?:\s+|$)")
NUMBER_RE = re.compile(r"\s*(\d{1,3})[.)](?:\s+|$)")
# 其他編號形式（a.、(1)、[1]、一、、1、…）：不轉成 Markdown 清單，但視為段落起點
ENUM_RE = re.compile(
    r"\s*(?:[A-Za-z][.)]\s|\(\w{1,3}\)|（[^）]{1,3}）|\[\d{1,3}\]"
    r"|[一二三四五六七八九十]{1,3}[、.．]|\d{1,3}[、．])"
)
# 頁碼：「12」「- 12 -」「Page 3 of 10」「3 / 10」「第 3 頁」「iv」
PAGE_NUM_RE = re.compile(
    r"(?:page\s*)?[-–—]?\s*\d{1,4}\s*[-–—]?(?:\s*(?:/|of)\s*\d{1,4})?"
    r"|第\s*\d{1,4}\s*[頁页](?:\s*[,，/]?\s*共\s*\d{1,4}\s*[頁页])?"
    r"|(?=[ivxlc]+$)c{0,3}(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})",
    re.IGNORECASE,
)
# 段落結尾若是這些字元，就不和下一頁開頭合併；粗體短句結尾若是這些字元，就不當作標題
SENTENCE_END = tuple(".!?。！？:：;；…」』\"”'’)）")
NOT_HEADING_END = tuple(":：,，;；.。")

BOLD_FONT_RE = re.compile(r"bold|black|heavy|demi|cmbx", re.IGNORECASE)
ITALIC_FONT_RE = re.compile(r"italic|oblique|cmti", re.IGNORECASE)
# 只依字型名稱判斷等寬字：許多中文字型（如細明體）也標記為等寬，不能用 flags 判斷
MONO_FONT_RE = re.compile(
    r"mono|courier|consol|menlo|inconsolata|cmtt|typewriter|code", re.IGNORECASE
)

_CHAR_MAP = str.maketrans({
    " ": " ", " ": " ", " ": " ", " ": " ", "\t": " ",
    "​": None, "﻿": None,
    # Symbol／Wingdings 字型使用私用區字元當項目符號
    "": "•", "": "▪", "": "➢", "": "❖",
    "": "✓", "": "■", "": "●", "": "□",
})
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_ESCAPE_RE = re.compile(r"([\\`*\[\]<~])")
_UNDERSCORE_RE = re.compile(r"(?<![0-9A-Za-z])_|_(?![0-9A-Za-z])")

APP_NAME = "JH PDF2MD"
MARGIN_RATIO = 0.1     # 頁面上下各 10% 視為頁首／頁尾區
MIN_IMAGE_SIZE = 20    # 寬或高小於此值（點）的圖片視為裝飾線條，略過


# ---------------------------------------------------------------------------
# 資料結構
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class Span:
    """樣式一致的一小段文字。"""
    text: str
    bold: bool = False
    italic: bool = False
    mono: bool = False
    link: str | None = None


@dataclass(slots=True)
class Line:
    """PDF 中的一個視覺行。"""
    spans: list[Span]
    rect: pymupdf.Rect
    size: float               # 主要字級（依字數加權，四捨五入到 0.5）
    block: int                # 所屬文字區塊的編號
    block_rect: pymupdf.Rect  # 所屬文字區塊的範圍

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def is_mono(self) -> bool:
        return all(s.mono for s in self.spans if s.text.strip())


@dataclass(slots=True)
class Block:
    """輸出的一個 Markdown 區塊。"""
    kind: str                 # text | heading | list | code | table | image | rule
    rect: pymupdf.Rect
    page: int
    size: float = 0.0
    level: int = 0            # 標題層級（0 表示不是標題）
    indent: int = 0           # 清單的巢狀層級
    enum: bool = False        # 段首為編號或項目符號（有懸掛縮排，不套用首行縮排規則）
    rows: int = 1             # 涵蓋的視覺行數
    spans: list[Span] = field(default_factory=list)
    code_rows: list[tuple[float, float, str]] = field(default_factory=list)  # (x0, 字寬, 文字)
    markdown: str = ""        # 表格、圖片等已產生好的 Markdown

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    def add_line(self, spans: list[Span], sep: str | None = None) -> None:
        """把一行文字接到段落後面；sep 為 None 時自動判斷接合方式。"""
        spans = strip_spans(spans)
        if not spans:
            return
        if self.spans:
            last = self.spans[-1]
            if sep is None:
                if is_hyphenated(last.text, spans[0].text):
                    last.text = last.text[:-1]
                    sep = ""
                else:
                    sep = join_sep(last.text, spans[0].text)
            last.text += sep
        self.spans.extend(spans)


@dataclass
class DocStats:
    """整份文件的版面統計，用來判斷標題與段落。"""
    body_size: float = 10.0
    heading_sizes: dict[float, int] = field(default_factory=dict)
    bold_heading_level: int = 0   # 單獨一行粗體短句的標題層級（0 表示停用）
    para_gap: float = 0.5         # 行距超過「字級 × 此值」視為新段落
    blank_gap: float = 0.8        # 程式碼行距超過「字級 × 此值」視為中間有空行

    def level(self, size: float) -> int:
        return self.heading_sizes.get(size, 0)


@dataclass
class Options:
    pages: str | None = None
    images: bool = True
    tables: bool = True
    keep_headers: bool = False
    page_breaks: bool = False
    sort: bool = False
    password: str | None = None


@dataclass
class ImageContext:
    folder: Path
    md_dir: Path
    skip: set[int]
    saved: dict[int, Path] = field(default_factory=dict)
    count: int = 0


# ---------------------------------------------------------------------------
# 文字工具
# ---------------------------------------------------------------------------

def clean_text(text: str) -> str:
    return _CTRL_RE.sub("", text.translate(_CHAR_MAP))


def join_sep(left: str, right: str) -> str:
    """決定兩段文字接合時是否需要補空白。"""
    if not left or not right or left[-1].isspace() or right[0].isspace():
        return ""
    if CJK_RE.match(left[-1]) or CJK_RE.match(right[0]):
        return ""
    return " "


def is_hyphenated(left: str, right: str) -> bool:
    """英文單字在行尾以連字號斷開（例如 conver- / sion）。"""
    return (
        len(left) >= 2 and left[-1] == "-" and left[-2].isalpha()
        and not CJK_RE.match(left[-2]) and right[:1].islower()
    )


def join_texts(parts: list[str]) -> str:
    out = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if is_hyphenated(out, part):
            out = out[:-1] + part
        else:
            out += join_sep(out, part) + part
    return out


def strip_spans(spans: list[Span]) -> list[Span]:
    """複製片段並去除首尾空白。"""
    out = [replace(s) for s in spans if s.text]
    while out and not out[0].text.strip():
        out.pop(0)
    while out and not out[-1].text.strip():
        out.pop()
    if out:
        out[0].text = out[0].text.lstrip()
        out[-1].text = out[-1].text.rstrip()
    return out


def drop_prefix(spans: list[Span], n: int) -> list[Span]:
    """移除開頭 n 個字元（例如清單符號）。"""
    out = []
    for s in spans:
        if n <= 0:
            out.append(s)
        elif len(s.text) <= n:
            n -= len(s.text)
        else:
            out.append(replace(s, text=s.text[n:]))
            n = 0
    return out


def escape_md(text: str) -> str:
    text = _ESCAPE_RE.sub(r"\\\1", text)
    return _UNDERSCORE_RE.sub(r"\\_", text)


def escape_block_start(md: str) -> str:
    """避免段落開頭被誤判為標題、引言、清單等 Markdown 語法。"""
    if re.match(r"[#>+=-]", md):
        return "\\" + md
    return re.sub(r"^(\d+)([.)])(?=\s|$)", r"\1\\\2", md)


def escape_url(url: str) -> str:
    return url.strip().replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def split_ws(text: str) -> tuple[str, str, str]:
    """拆成（前導空白、內容、結尾空白）。"""
    core = text.strip()
    if not core:
        return text, "", ""
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return lead, core, trail


def code_span(text: str) -> str:
    ticks = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (ticks + 1)
    pad = " " if ticks else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def format_ranges(nums: list[int]) -> str:
    """[1, 2, 3, 7] -> "1-3, 7" """
    parts = []
    for _, run in groupby(enumerate(nums), key=lambda p: p[1] - p[0]):
        run = [n for _, n in run]
        parts.append(str(run[0]) if len(run) == 1 else f"{run[0]}-{run[-1]}")
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# 第一階段：擷取文字行
# ---------------------------------------------------------------------------

def split_by_links(chars: list[dict], links: list[tuple[pymupdf.Rect, str]]) -> list[list]:
    """依超連結範圍把一個 span 的字元切成 [[文字, 網址或 None], ...]。"""
    pieces: list[list] = []
    for ch in chars:
        x0, y0, x1, y1 = ch["bbox"]
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        uri = next((u for r, u in links if r.x0 <= cx <= r.x1 and r.y0 <= cy <= r.y1), None)
        if pieces and pieces[-1][1] == uri:
            pieces[-1][0] += ch["c"]
        else:
            pieces.append([ch["c"], uri])
    return pieces


def extract_lines(page: pymupdf.Page, sort: bool = False) -> list[Line]:
    links = [(pymupdf.Rect(l["from"]), l["uri"]) for l in page.get_links() if l.get("uri")]
    # 有連結的頁面需要逐字元座標，才能精確標出連結涵蓋的文字
    mode = "rawdict" if links else "dict"
    data = page.get_text(mode, flags=pymupdf.TEXT_MEDIABOX_CLIP, sort=sort)
    lines = []
    for bno, block in enumerate(data["blocks"]):
        if block.get("type") != 0:
            continue
        block_rect = pymupdf.Rect(block["bbox"])
        for ln in block["lines"]:
            dx, dy = ln["dir"]
            if abs(dy) > 0.1 or dx < 0:  # 略過旋轉或垂直的文字（多為浮水印、側邊標籤）
                continue
            spans = []
            weights = Counter()
            for s in ln["spans"]:
                font, flags = s["font"], s["flags"]
                bold = bool(flags & 16 or BOLD_FONT_RE.search(font))
                italic = bool(flags & 2 or ITALIC_FONT_RE.search(font))
                mono = bool(MONO_FONT_RE.search(font))
                pieces = split_by_links(s["chars"], links) if "chars" in s else [[s["text"], None]]
                for raw, uri in pieces:
                    text = clean_text(raw)
                    if not text:
                        continue
                    spans.append(Span(text, bold, italic, mono, uri))
                    n = len(text.strip())
                    if n:
                        weights[round(s["size"] * 2) / 2] += n
            if not weights:
                continue
            # 軟連字號只在行尾有意義（代表斷字），其他位置直接移除
            tail = spans[-1].text.rstrip()
            if tail.endswith("­"):
                spans[-1].text = tail[:-1] + "-"
            for s in spans:
                s.text = s.text.replace("­", "")
            size = weights.most_common(1)[0][0]
            lines.append(Line(spans, pymupdf.Rect(ln["bbox"]), size, bno, block_rect))
    return lines


def remove_headers_footers(
    pages: dict[int, list[Line]], rects: dict[int, pymupdf.Rect]
) -> dict[int, list[Line]]:
    """移除頁首頁尾區內重複出現的文字與頁碼。"""

    def in_margin(line: Line, rect: pymupdf.Rect) -> bool:
        h = rect.height * MARGIN_RATIO
        return line.rect.y1 <= rect.y0 + h or line.rect.y0 >= rect.y1 - h

    def normalize(text: str) -> str:
        return re.sub(r"\d+", "#", re.sub(r"\s+", " ", text.strip().lower()))

    counts = Counter()
    for pno, lines in pages.items():
        counts.update({normalize(l.text) for l in lines if in_margin(l, rects[pno])})
    repeated = set()
    if len(pages) >= 2:
        threshold = max(2, 0.4 * len(pages))
        repeated = {k for k, c in counts.items() if c >= threshold}

    result = {}
    for pno, lines in pages.items():
        kept = []
        for line in lines:
            if in_margin(line, rects[pno]):
                text = line.text.strip()
                if normalize(text) in repeated or PAGE_NUM_RE.fullmatch(text):
                    continue
            kept.append(line)
        result[pno] = kept
    return result


def analyze(pages: dict[int, list[Line]]) -> DocStats:
    """統計內文字級、標題字級與一般行距。"""
    sizes = Counter()
    gaps = []
    for lines in pages.values():
        prev = None
        for line in lines:
            sizes[line.size] += len(line.text.strip())
            if (prev and prev.block == line.block and prev.size == line.size
                    and line.rect.y0 >= prev.rect.y0 + 0.5 * line.size):
                gaps.append((line.rect.y0 - prev.rect.y1) / line.size)
            prev = line
    if not sizes:
        return DocStats()

    body = sizes.most_common(1)[0][0]
    bigger = sorted((s for s in sizes if s >= body + max(1.0, body * 0.12)), reverse=True)
    heading_sizes = {s: min(i + 1, 6) for i, s in enumerate(bigger)}

    # 若內文大量使用粗體，就不把粗體短句當作標題
    body_chars = bold_chars = 0
    for lines in pages.values():
        for line in lines:
            if line.size == body:
                for s in line.spans:
                    n = len(s.text.strip())
                    body_chars += n
                    bold_chars += n if s.bold else 0
    bold_level = min(len(bigger) + 1, 6) if bold_chars < 0.3 * body_chars else 0

    line_gap = statistics.median(gaps) if gaps else 0.1
    line_gap = min(max(line_gap, -0.5), 1.0)
    return DocStats(
        body_size=body,
        heading_sizes=heading_sizes,
        bold_heading_level=bold_level,
        para_gap=max(line_gap + 0.4, 0.3),
        blank_gap=line_gap + 0.6,
    )


# ---------------------------------------------------------------------------
# 第二階段：把文字行組成段落
# ---------------------------------------------------------------------------

def same_row(a: Line, b: Line) -> bool:
    """b 是否與 a 位在同一個視覺行，且在 a 的右側。"""
    overlap = min(a.rect.y1, b.rect.y1) - max(a.rect.y0, b.rect.y0)
    return (overlap > 0.5 * min(a.rect.height, b.rect.height)
            and b.rect.x0 >= a.rect.x1 - 0.5 * b.size)


def code_row(line: Line) -> tuple[float, float, str]:
    text = line.text.rstrip()
    return line.rect.x0, (line.rect.width / len(text) if text else 0.0), text


def new_block(line: Line, level: int, is_code: bool, page_no: int) -> Block:
    block = Block("text", pymupdf.Rect(line.rect), page_no, size=line.size, level=level)
    if is_code:
        block.kind = "code"
        block.code_rows.append(code_row(line))
        return block
    text = line.text
    if level:
        block.kind = "heading"
    elif BULLET_RE.match(text) or NUMBER_RE.match(text):
        block.kind = "list"
    block.enum = block.kind == "list" or bool(ENUM_RE.match(text))
    block.add_line(line.spans)
    return block


def starts_new(cur: Block, prev: Line, line: Line, level: int, is_code: bool,
               stats: DocStats) -> bool:
    """判斷 line 是否開始一個新區塊（而不是接續 cur）。"""
    size = line.size
    if cur.kind == "code" or is_code:
        if cur.kind != "code" or not is_code:
            return True
        return line.rect.y0 < prev.rect.y0 or line.rect.y0 - prev.rect.y1 > 3 * size
    if level != cur.level:
        return True
    br = prev.block_rect
    if line.rect.y0 < prev.rect.y0 - 0.5 * size or line.rect.x0 >= br.x1 or line.rect.x1 <= br.x0:
        return True  # 往上跳或離開原區塊：換欄、側欄等版面切換
    if line.rect.y0 - prev.rect.y1 > stats.para_gap * size:
        return True
    text = line.text
    short = br.x1 - prev.rect.x1 > max(0.25 * br.width, 6 * size)  # 上一行提前結束
    m = BULLET_RE.match(text)
    if m:
        # 「-」「–」也可能只是換行後的破折號，上一行提前結束才視為新的清單項目
        return m.group(1) not in "-–" or short or cur.kind == "list"
    if NUMBER_RE.match(text) or ENUM_RE.match(text):
        return True
    if level:
        return False  # 標題折成多行
    if abs(size - prev.size) >= 1 or short:
        return True
    return not cur.enum and line.rect.x0 > prev.rect.x0 + size  # 首行縮排


def build_blocks(lines: list[Line], stats: DocStats, page_no: int) -> list[Block]:
    blocks: list[Block] = []
    cur: Block | None = None
    prev: Line | None = None
    for line in lines:
        size = line.size
        if cur is not None and same_row(prev, line):
            # 同一行被拆成多段（例如項目符號與內文分開、表格欄位）
            gap = line.rect.x0 - prev.rect.x1
            if cur.kind == "code":
                x0, cw, text = cur.code_rows[-1]
                spaces = max(1, round(gap / cw)) if cw > 0 and gap > 0.25 * size else 0
                cur.code_rows[-1] = (x0, cw, text + " " * spaces + line.text.strip())
            else:
                cur.add_line(line.spans, " " if gap > 0.25 * size else "")
            cur.rect |= line.rect
            prev = Line(prev.spans + line.spans, prev.rect | line.rect, prev.size,
                        prev.block, prev.block_rect | line.block_rect)
            continue

        level = stats.level(size)
        is_code = not level and line.is_mono
        if cur is None or starts_new(cur, prev, line, level, is_code, stats):
            cur = new_block(line, level, is_code, page_no)
            blocks.append(cur)
        else:
            if cur.kind == "code":
                if line.rect.y0 - prev.rect.y1 > stats.blank_gap * size:
                    cur.code_rows.append((line.rect.x0, 0.0, ""))
                cur.code_rows.append(code_row(line))
            else:
                cur.add_line(line.spans)
            cur.rect |= line.rect
            cur.rows += 1
        prev = line
    return blocks


# ---------------------------------------------------------------------------
# 表格與圖片
# ---------------------------------------------------------------------------

def cell_text(cell: str | None) -> str:
    if not cell:
        return ""
    text = join_texts(clean_text(cell).split("\n"))
    return escape_md(text).replace("|", "\\|")


def table_to_markdown(table) -> str:
    if table.row_count < 2 or table.col_count < 2:
        return ""
    # 一律以第一列為表頭：PyMuPDF 的「表格外表頭」判斷常把表格上方的標題誤認為表頭
    rows = [[cell_text(c) for c in row] for row in table.extract()]
    rows = [r for r in rows if any(r)]
    if not rows:
        return ""
    ncols = max(map(len, rows))
    rows = [r + [""] * (ncols - len(r)) for r in rows]
    keep = [j for j in range(ncols) if any(r[j] for r in rows)]
    if len(keep) < 2:  # 實際上只有一欄（多半是外框），當成一般文字處理
        return ""
    rows = [[r[j] for j in keep] for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join("---" for _ in keep) + " |"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def has_vector_lines(page: pymupdf.Page) -> bool:
    """頁面上是否有足以構成框線表格的線條或色塊（檢查成本遠低於 find_tables）。"""
    try:
        return sum(len(d.get("items", ())) for d in page.get_cdrawings()) >= 3
    except Exception:
        return True


def extract_tables(page: pymupdf.Page) -> list[Block]:
    if not has_vector_lines(page):
        return []
    try:
        found = page.find_tables()
    except Exception:
        return []
    blocks = []
    # 注意：table.extract() 依賴最近一次 find_tables() 的結果，必須立刻處理完這一頁
    for table in found.tables:
        try:
            md = table_to_markdown(table)
        except Exception:
            continue
        if md:
            blocks.append(Block("table", pymupdf.Rect(table.bbox), page.number, markdown=md))
    return blocks


def center_in(rect: pymupdf.Rect, r: pymupdf.Rect) -> bool:
    cx, cy = (r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2
    return rect.x0 - 1 <= cx <= rect.x1 + 1 and rect.y0 - 1 <= cy <= rect.y1 + 1


def save_image(doc: pymupdf.Document, page: pymupdf.Page, xref: int,
               rect: pymupdf.Rect, base: Path) -> Path | None:
    """儲存圖片；能直接取出原始 PNG/JPEG 就不重新編碼，否則轉成 PNG。"""
    base.parent.mkdir(parents=True, exist_ok=True)
    if xref:
        try:
            info = doc.extract_image(xref) or {}
            if (info.get("ext") in ("png", "jpeg", "jpg") and not info.get("smask")
                    and info.get("colorspace") in (1, 3)):
                path = base.with_suffix(".png" if info["ext"] == "png" else ".jpg")
                path.write_bytes(info["image"])
                return path
            pix = pymupdf.Pixmap(doc, xref)
            if pix.colorspace and pix.colorspace.n > 3:  # CMYK 等轉成 RGB
                pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
            if info.get("smask"):  # 套用透明遮罩
                pix = pymupdf.Pixmap(pix, pymupdf.Pixmap(doc, info["smask"]))
            path = base.with_suffix(".png")
            pix.save(path)
            return path
        except Exception:
            pass  # 改用下面的方式：直接把該區域渲染成圖片
    try:
        path = base.with_suffix(".png")
        page.get_pixmap(clip=rect, dpi=150).save(path)
        return path
    except Exception:
        return None


def image_link(path: Path, md_dir: Path) -> str:
    rel = Path(os.path.relpath(path, md_dir)).as_posix()
    return f"<{rel}>" if re.search(r"[\s()]", rel) else rel


def repeated_images(images: dict[int, list[dict]]) -> set[int]:
    """出現在大多數頁面的圖片（例如頁首 Logo）。"""
    if len(images) < 3:
        return set()
    counts = Counter()
    for infos in images.values():
        counts.update({i["xref"] for i in infos if i.get("xref")})
    threshold = max(3, 0.5 * len(images))
    return {xref for xref, c in counts.items() if c >= threshold}


def extract_images(doc: pymupdf.Document, page: pymupdf.Page, infos: list[dict],
                   ctx: ImageContext, has_text: bool) -> list[Block]:
    blocks = []
    page_area = page.rect.get_area()
    n = 0
    for info in infos:
        rect = pymupdf.Rect(info["bbox"]) & page.rect
        if rect.is_empty or rect.width < MIN_IMAGE_SIZE or rect.height < MIN_IMAGE_SIZE:
            continue
        xref = info.get("xref", 0)
        if xref in ctx.skip:
            continue
        if has_text and rect.get_area() > 0.9 * page_area:
            continue  # 有文字的頁面上鋪滿整頁的圖片，多半是背景或掃描底圖
        path = ctx.saved.get(xref) if xref else None
        if path is None:
            n += 1
            path = save_image(doc, page, xref, rect, ctx.folder / f"page{page.number + 1}_{n}")
            if path is None:
                continue
            ctx.count += 1
            if xref:
                ctx.saved[xref] = path
        md = f"![{path.stem}]({image_link(path, ctx.md_dir)})"
        blocks.append(Block("image", rect, page.number, markdown=md))
    return blocks


def place_inserts(blocks: list[Block], inserts: list[Block]) -> list[Block]:
    """依位置把表格、圖片插入文字段落之間。"""
    result = list(blocks)
    for ins in sorted(inserts, key=lambda b: (b.rect.y0, b.rect.x0)):
        r = ins.rect

        def below(b: Block, need_overlap: bool) -> bool:
            if b.kind in ("table", "image") or b.rect.y0 < r.y0 - 1:
                return False
            return not need_overlap or (b.rect.x0 < r.x1 and b.rect.x1 > r.x0)

        idx = next((i for i, b in enumerate(result) if below(b, True)), None)
        if idx is None:
            idx = next((i for i, b in enumerate(result) if below(b, False)), len(result))
        result.insert(idx, ins)
    return result


# ---------------------------------------------------------------------------
# 後處理
# ---------------------------------------------------------------------------

def finalize_block(b: Block, stats: DocStats) -> None:
    text = b.text.strip()
    if b.kind == "heading":
        if len(text) > 200 or not LETTER_RE.search(text):
            b.kind, b.level = "text", 0
    elif (b.kind == "text" and stats.bold_heading_level and b.rows == 1
          and b.size >= stats.body_size - 0.5 and 0 < len(text) <= 80
          and all(s.bold for s in b.spans if s.text.strip())
          and not text.endswith(NOT_HEADING_END) and LETTER_RE.search(text)):
        b.kind, b.level = "heading", stats.bold_heading_level


def merge_across_pages(blocks: list[Block]) -> list[Block]:
    """把被分頁切斷的段落接回去。"""
    out: list[Block] = []
    for b in blocks:
        p = out[-1] if out else None
        if (p is not None and b.kind == "text" and not b.enum and p.kind in ("text", "list")
                and b.page == p.page + 1 and p.rows >= 2):
            left, right = p.text.rstrip(), b.text.lstrip()
            if (left and right and not left.endswith(SENTENCE_END)
                    and (right[0].islower() or CJK_RE.match(right[0]))):
                p.add_line(b.spans)
                p.rows += b.rows
                continue
        out.append(b)
    return out


def assign_list_levels(blocks: list[Block]) -> None:
    """依項目符號的水平位置決定巢狀層級。"""
    stack: list[float] = []
    for b in blocks:
        if b.kind != "list":
            stack.clear()
            continue
        x = b.rect.x0
        while stack and x < stack[-1] - 3:
            stack.pop()
        if not stack or x > stack[-1] + 3:
            stack.append(x)
        b.indent = min(len(stack) - 1, 5)


# ---------------------------------------------------------------------------
# 輸出 Markdown
# ---------------------------------------------------------------------------

def render_styles(spans: list[Span], styled: bool) -> str:
    groups: list[list] = []
    for s in spans:
        key = (s.bold and styled, s.italic and styled, s.mono)
        if groups and (groups[-1][0] == key or not s.text.strip()):
            groups[-1][1] += s.text
        else:
            groups.append([key, s.text])
    out = []
    for (bold, italic, mono), text in groups:
        lead, core, trail = split_ws(text)
        if not core:
            out.append(" " if text else "")
            continue
        if mono:
            core = code_span(core)
        else:
            core = escape_md(core)
            if bold and italic:
                core = f"***{core}***"
            elif bold:
                core = f"**{core}**"
            elif italic:
                core = f"*{core}*"
        out.append((" " if lead else "") + core + (" " if trail else ""))
    return "".join(out)


def render_spans(spans: list[Span], styled: bool = True) -> str:
    """把片段轉成行內 Markdown（粗體、斜體、行內程式碼、連結）。"""
    parts = []
    for link, group in groupby(spans, key=lambda s: s.link):
        md = render_styles(list(group), styled)
        if link and md.strip():
            lead, core, trail = split_ws(md)
            md = f"{lead}[{core}]({escape_url(link)}){trail}"
        parts.append(md)
    return re.sub(r" {2,}", " ", "".join(parts)).strip()


def render_code(b: Block) -> str:
    real = [(x, cw) for x, cw, t in b.code_rows if t.strip()]
    if not real:
        return ""
    left = min(x for x, _ in real)
    widths = [cw for _, cw in real if cw > 0]
    cw = statistics.median(widths) if widths else 1.0
    lines = [
        " " * max(0, round((x - left) / cw)) + t if t.strip() else ""
        for x, _, t in b.code_rows
    ]
    body = "\n".join(lines).strip("\n")
    fence = "~~~" if "```" in body else "```"
    return f"{fence}\n{body}\n{fence}"


def render_list(b: Block) -> str:
    text = b.text
    m = NUMBER_RE.match(text)
    marker = f"{m.group(1)}." if m else "-"
    m = m or BULLET_RE.match(text)
    if not m:
        return escape_block_start(render_spans(b.spans))
    body = render_spans(drop_prefix(b.spans, m.end()))
    if not body:
        return ""
    return "    " * b.indent + f"{marker} {escape_block_start(body)}"


def render_block(b: Block) -> str:
    if b.kind in ("table", "image", "rule"):
        return b.markdown
    if b.kind == "code":
        return render_code(b)
    if b.kind == "heading":
        body = render_spans(b.spans, styled=False)
        return f"{'#' * b.level} {body}" if body else ""
    if b.kind == "list":
        return render_list(b)
    return escape_block_start(render_spans(b.spans))


def render(blocks: list[Block]) -> str:
    parts = []
    prev_list = None  # 上一個區塊若是清單項目，記錄它是否為編號清單
    for b in blocks:
        md = render_block(b)
        if not md:
            continue
        cur_list = bool(NUMBER_RE.match(b.text)) if b.kind == "list" else None
        if parts:
            # 同類型的清單項目之間不空行；項目清單與編號清單交界處要空行，避免黏成同一個清單
            same_list = cur_list is not None and cur_list == prev_list
            parts.append("\n" if same_list else "\n\n")
        parts.append(md)
        prev_list = cur_list
    return "".join(parts).rstrip() + "\n"


# ---------------------------------------------------------------------------
# 轉換流程
# ---------------------------------------------------------------------------

def parse_pages(spec: str | None, count: int) -> list[int]:
    """把「1-3,5,8-」轉成從 0 起算的頁碼清單。"""
    if not spec:
        return list(range(count))
    result: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)|(\d+)", part)
        if not m:
            raise ValueError(f"無法解析頁碼範圍：{part}")
        if m.group(3):
            start = end = int(m.group(3))
        else:
            start, end = int(m.group(1) or 1), int(m.group(2) or count)
        for p in range(max(start, 1), min(end, count) + 1):
            if p - 1 not in result:
                result.append(p - 1)
    if not result:
        raise ValueError(f"指定的頁碼超出範圍（本文件共 {count} 頁）")
    return result


def convert(pdf_path: Path, md_path: Path, opts: Options) -> dict:
    doc = pymupdf.open(pdf_path)
    try:
        if doc.needs_pass and not doc.authenticate(opts.password or ""):
            raise ValueError("PDF 已加密，請用 --password 提供正確密碼")
        page_nos = parse_pages(opts.pages, doc.page_count)

        # 第一輪：擷取所有頁面的文字行，統計整份文件的版面特徵
        pages: dict[int, list[Line]] = {}
        rects: dict[int, pymupdf.Rect] = {}
        images: dict[int, list[dict]] = {}
        for pno in page_nos:
            page = doc[pno]
            pages[pno] = extract_lines(page, opts.sort)
            rects[pno] = page.rect
            if opts.images:
                images[pno] = page.get_image_info(xrefs=True)
        no_text = [pno + 1 for pno in page_nos if not pages[pno]]
        if not opts.keep_headers:
            pages = remove_headers_footers(pages, rects)
        stats = analyze(pages)
        ctx = ImageContext(
            folder=md_path.parent / f"{md_path.stem}_images",
            md_dir=md_path.parent,
            skip=set() if opts.keep_headers else repeated_images(images),
        )

        # 第二輪：逐頁組成段落，並插入表格與圖片
        blocks: list[Block] = []
        n_tables = 0
        for i, pno in enumerate(page_nos):
            page = doc[pno]
            lines = pages[pno]
            inserts: list[Block] = []
            if opts.tables:
                tables = extract_tables(page)
                n_tables += len(tables)
                inserts += tables
                lines = [l for l in lines if not any(center_in(t.rect, l.rect) for t in tables)]
            page_blocks = build_blocks(lines, stats, pno)
            if opts.images:
                inserts += extract_images(doc, page, images[pno], ctx, has_text=bool(pages[pno]))
            if opts.page_breaks and i > 0:
                blocks.append(Block("rule", pymupdf.Rect(), pno, markdown="---"))
            blocks.extend(place_inserts(page_blocks, inserts))

        for b in blocks:
            finalize_block(b, stats)
        blocks = merge_across_pages(blocks)
        assign_list_levels(blocks)

        md_path.parent.mkdir(parents=True, exist_ok=True)
        md_path.write_text(render(blocks), encoding="utf-8")
        return {"pages": len(page_nos), "images": ctx.count, "tables": n_tables,
                "no_text": no_text}
    finally:
        doc.close()


def collect_jobs(inputs: list[str], recursive: bool) -> tuple[list[tuple[Path, Path]], list[str]]:
    """回傳 [(PDF 路徑, 輸出用的相對路徑)] 與找不到的輸入。"""
    jobs, missing = [], []
    for raw in inputs:
        path = Path(raw)
        if path.is_dir():
            candidates = path.rglob("*") if recursive else path.iterdir()
            for f in sorted(candidates):
                if f.is_file() and f.suffix.lower() == ".pdf":
                    jobs.append((f, f.relative_to(path)))
        elif path.is_file():
            jobs.append((path, Path(path.name)))
        else:
            missing.append(raw)
    return jobs, missing


def owns_console() -> bool:
    """主控台視窗是否專為本程式開啟（從檔案總管雙擊或拖放啟動，而非在終端機中執行）。"""
    if os.name != "nt":
        return False
    try:
        import ctypes
        pids = (ctypes.c_uint * 8)()
        count = ctypes.windll.kernel32.GetConsoleProcessList(pids, 8)
    except Exception:
        return False
    # PyInstaller 單一執行檔會有啟動器與主程式兩個行程共用主控台
    meipass = getattr(sys, "_MEIPASS", None)
    onefile = meipass is not None and Path(meipass).parent != Path(sys.executable).parent
    return 0 < count <= (2 if onefile else 1)


def pick_files() -> list[str]:
    """跳出視窗讓使用者選擇 PDF；無法使用圖形介面時改為輸入路徑。"""
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        files = filedialog.askopenfilenames(
            parent=root,
            title=f"{APP_NAME}：選擇要轉換的 PDF",
            filetypes=[("PDF 檔案", "*.pdf"), ("所有檔案", "*.*")],
        )
        root.destroy()
        return list(files)
    except Exception:
        try:
            raw = input("請輸入 PDF 檔案或資料夾的路徑（可直接拖曳到此視窗）：")
        except EOFError:
            return []
        raw = raw.strip().strip('"')
        return [raw] if raw else []


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass

    if argv is None:
        argv = sys.argv[1:]
    if not argv and owns_console():
        # 直接雙擊啟動、沒有任何參數：改用選檔視窗
        print(f"{APP_NAME}：PDF 轉 Markdown")
        print("請在跳出的視窗中選擇要轉換的 PDF（可多選）。")
        print("也可以把 PDF 檔或資料夾直接拖曳到本程式的圖示上。\n")
        argv = pick_files()
        if not argv:
            print("沒有選擇任何檔案。")
            return 0

    parser = argparse.ArgumentParser(
        description="將 PDF 轉換為 Markdown。",
        epilog="範例：%(prog)s 報告.pdf -o 報告.md --pages 1-10",
    )
    parser.add_argument("inputs", nargs="+", help="PDF 檔案，或包含 PDF 的資料夾")
    parser.add_argument("-o", "--output",
                        help="輸出的 .md 檔；有多個輸入時為輸出資料夾（預設：與 PDF 同資料夾）")
    parser.add_argument("-r", "--recursive", action="store_true", help="遞迴處理子資料夾中的 PDF")
    parser.add_argument("--pages", help="只轉換指定頁面，例如 1-3,5,10-")
    parser.add_argument("--no-images", action="store_true", help="不擷取圖片")
    parser.add_argument("--no-tables", action="store_true", help="不偵測表格（可加快速度）")
    parser.add_argument("--keep-headers", action="store_true", help="保留頁首、頁尾與頁碼")
    parser.add_argument("--page-breaks", action="store_true", help="在每頁之間插入分隔線 ---")
    parser.add_argument("--sort", action="store_true",
                        help="依版面位置（由上而下、由左而右）重新排序文字，"
                             "適用於文字順序錯亂的 PDF；多欄排版請勿使用")
    parser.add_argument("--password", help="加密 PDF 的密碼")
    args = parser.parse_args(argv)

    jobs, missing = collect_jobs(args.inputs, args.recursive)
    for raw in missing:
        print(f"找不到：{raw}", file=sys.stderr)
    if not jobs:
        print("沒有可轉換的 PDF。", file=sys.stderr)
        return 1

    opts = Options(
        pages=args.pages,
        images=not args.no_images,
        tables=not args.no_tables,
        keep_headers=args.keep_headers,
        page_breaks=args.page_breaks,
        sort=args.sort,
        password=args.password,
    )
    out = Path(args.output) if args.output else None
    single_file = out is not None and len(jobs) == 1 and out.suffix.lower() == ".md"

    failures = len(missing)
    for pdf, rel in jobs:
        if out is None:
            md = pdf.with_suffix(".md")
        elif single_file:
            md = out
        else:
            md = out / rel.with_suffix(".md")
        try:
            info = convert(pdf, md, opts)
        except Exception as e:
            print(f"失敗：{pdf}：{e}", file=sys.stderr)
            failures += 1
            continue
        print(f"完成：{pdf} -> {md}"
              f"（{info['pages']} 頁、{info['tables']} 個表格、{info['images']} 張圖片）")
        if info["no_text"]:
            print(f"  注意：第 {format_ranges(info['no_text'])} 頁沒有可擷取的文字，"
                  "可能是掃描影像，需要先經過 OCR 處理。", file=sys.stderr)
    return 1 if failures else 0


def run() -> int:
    """程式進入點：主控台若是專為本程式開啟的，結束前先暫停，讓使用者看得到結果。"""
    own = owns_console()
    if own:
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleTitleW(APP_NAME)
        except Exception:
            pass
    try:
        code = main()
    except SystemExit as e:  # argparse 的參數錯誤或 --help
        code = e.code if isinstance(e.code, int) else int(e.code is not None)
    except KeyboardInterrupt:
        code = 130
    except Exception:
        import traceback
        traceback.print_exc()
        code = 1
    if own and sys.stdin is not None and sys.stdin.isatty():
        try:
            input("\n按 Enter 鍵結束…")
        except (EOFError, KeyboardInterrupt):
            pass
    return code


if __name__ == "__main__":
    sys.exit(run())
