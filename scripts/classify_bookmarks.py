#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
classify_bookmarks.py — 书签中括号前缀分类标注器（可复用）

用途
----
读取任意 Netscape 格式的书签 HTML 文件，依据 bookmark_taxonomy.json 定义的中括号
前缀分类体系，为「每一个」书签自动添加形如【类型】的前缀标签；用户已有的中括号
前缀原样保留。输出一份新的书签 HTML（不覆盖输入），并打印覆盖统计报告。

这是一套「可扩展体系」的执行引擎：换一个书签文件、改一份 taxonomy，即可复用。

用法
----
  python3 classify_bookmarks.py \
      --input  bookmarks.html \
      --taxonomy default_bookmark_taxonomy.json \
      --output bookmarks_classified.html \
      [--overrides overrides.json] \
      [--report   report.txt]

overrides.json 格式（可选，逐条强制指定，优先级最高）：
  { "https://example.com/a": "【字体】", "https://example.com/b": "【AI】" }

优先级（见 taxonomy.precedence）：
  1. 用户已有中括号前缀  -> 保留
  2. subfolder_map（顶层/子层精确匹配）
  3. folder_map（仅顶层匹配）
  4. keyword_refine（按所在文件夹做关键词细化）
  5. default_tag（兜底【其他】）；根级书签用 root_tag（【快捷】）
  *. overrides.json 中的条目最终覆盖以上所有结果
"""
import argparse
import json
import re
import sys
from html.parser import HTMLParser
from collections import Counter, defaultdict


# ----------------------------- 解析器：获取 书签+文件夹路径 -----------------------------
class BookmarkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.folder_stack = []
        self.pending = None          # 正在读取的文件夹名（H3 内）
        self.in_a = False
        self.a_text = ""
        self.a_href = None
        self.a_path = None
        self.bookmarks = []          # list of dict(href, name, path)

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "h3":
            self.pending = ""
        elif tag == "dl":
            if self.pending is not None:
                self.folder_stack.append(self.pending.strip())
                self.pending = None
        elif tag == "a" and "href" in d:
            self.in_a = True
            self.a_text = ""
            self.a_href = d["href"]
            self.a_path = list(self.folder_stack)

    def handle_endtag(self, tag):
        if tag == "dl" and self.pending is None and self.folder_stack:
            self.folder_stack.pop()
        elif tag == "a" and self.in_a:
            self.bookmarks.append({
                "href": self.a_href,
                "name": self.a_text.strip(),
                "path": self.a_path,
            })
            self.in_a = False

    def handle_data(self, data):
        if self.pending is not None:
            self.pending += data
        elif self.in_a:
            self.a_text += data


# ----------------------------- 工具函数 -----------------------------
LEADING_BRACKET = re.compile(r"^\s*([\[【])")


def has_user_prefix(name: str) -> bool:
    return bool(LEADING_BRACKET.match(name or ""))


def classify(name: str, url: str, path, taxo: dict):
    """返回该书的签应带的中括号前缀（含【】），或 None 表示保留现状。"""
    # 1. 用户已有前缀 -> 保留
    if has_user_prefix(name):
        return None

    top = path[0] if path else ""
    full_key = "/".join(path)

    # 2. subfolder_map 精确匹配（顶层/子层）
    sf = taxo.get("subfolder_map", {})
    if full_key in sf:
        return sf[full_key]

    # 3. folder_map 仅顶层
    fm = taxo.get("folder_map", {})
    base = fm.get(top, None)

    # 4. keyword_refine：仅在该顶层文件夹对应的关键词表里细化
    kr = taxo.get("keyword_refine", {}).get(top, {})
    if kr:
        hay = (name + " " + url).lower()
        for kw, tag in kr.items():
            if kw.lower() in hay:
                return tag  # 细化优先于 folder 默认

    # 5. folder 默认
    if base:
        return base

    # 根级 / 兜底
    if not top:
        return taxo.get("root_tag", "【快捷】")
    return taxo.get("default_tag", "【其他】")


def apply_prefix(tag: str, name: str) -> str:
    if not tag:
        return name
    return tag + name


# ----------------------------- 主流程 -----------------------------
def main():
    ap = argparse.ArgumentParser(description="书签中括号前缀分类标注器")
    ap.add_argument("--input", required=True, help="输入书签 HTML")
    ap.add_argument("--taxonomy", required=True, help="分类体系 JSON")
    ap.add_argument("--output", required=True, help="输出书签 HTML")
    ap.add_argument("--overrides", default=None, help="可选：逐条强制覆盖 JSON")
    ap.add_argument("--report", default=None, help="可选：覆盖统计报告输出路径")
    args = ap.parse_args()

    with open(args.taxonomy, encoding="utf-8") as f:
        taxo = json.load(f)
    with open(args.input, encoding="utf-8") as f:
        html = f.read()

    overrides = {}
    if args.overrides:
        with open(args.overrides, encoding="utf-8") as f:
            overrides = json.load(f)

    parser = BookmarkParser()
    parser.feed(html)

    # 计算 每个 href 的目标前缀（同名多书签用同一前缀）
    href_tag = {}
    kept = 0
    for b in parser.bookmarks:
        href = b["href"]
        name = b["name"]
        if has_user_prefix(name):
            href_tag[href] = ("KEEP",)  # 标记保留
            kept += 1
            continue
        tag = classify(name, href, b["path"], taxo)
        if href in overrides:
            tag = overrides[href]
        href_tag[href] = ("TAG", tag)

    # 单次全局正则：捕获 (属性前, href原始值, 引号, 属性后, 书名)，回调按 href 查表替换。
    # 兼容属性任意顺序、href 中的 &amp; 实体、以及 URL 自带的 () 等特殊字符。
    stats = Counter()
    changed = 0
    other_samples = []
    root_samples = []

    pat = re.compile(r'<A\b([^>]*?\bHREF=")([^"]*)(")([^>]*)>([^<]*)</A>')

    def repl(m):
        nonlocal changed
        raw_href = m.group(2)
        href = raw_href.replace("&amp;", "&").replace("&#38;", "&")  # 反转义用于查表
        g = href_tag.get(href)
        if g is None or g[0] == "KEEP":
            return m.group(0)
        tag = g[1]
        raw_name = m.group(5)          # 原始书名文本（保留 &amp; 等实体）
        new_name = tag + raw_name
        changed += 1
        stats[tag] += 1
        if tag == taxo.get("default_tag", "【其他】"):
            other_samples.append((href, raw_name))
        if tag == taxo.get("root_tag", "【快捷】"):
            root_samples.append((href, raw_name))
        return f'<A{m.group(1)}{raw_href}{m.group(3)}{m.group(4)}>{new_name}</A>'

    out_html = pat.sub(repl, html)

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(out_html)

    total = len(parser.bookmarks)
    # 报告
    lines = []
    lines.append("=" * 60)
    lines.append("书签中括号前缀分类 · 覆盖报告")
    lines.append("=" * 60)
    lines.append(f"输入文件      : {args.input}")
    lines.append(f"输出文件      : {args.output}")
    lines.append(f"书签总数      : {total}")
    lines.append(f"新增前缀数    : {changed}")
    lines.append(f"保留用户前缀  : {kept}")
    lines.append(f"覆盖比例      : {100.0*changed/total:.1f}% (新增) + 用户已有 {100.0*kept/total:.1f}%")
    lines.append("")
    lines.append("--- 各前缀数量 ---")
    for tag, c in stats.most_common():
        lines.append(f"  {c:4d}  {tag}")
    if other_samples:
        lines.append("")
        lines.append(f"--- 归入【其他】待人工复核 ({len(other_samples)}) ---")
        for href, name in other_samples[:40]:
            lines.append(f"  {name[:40]}  <-  {href}")
    if root_samples:
        lines.append("")
        lines.append(f"--- 根级【快捷】书签 ({len(root_samples)}) ---")
        for href, name in root_samples[:40]:
            lines.append(f"  {name[:40]}  <-  {href}")
    report_text = "\n".join(lines)

    print(report_text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            f.write(report_text + "\n")
        print(f"\n报告已写入: {args.report}")


if __name__ == "__main__":
    main()
