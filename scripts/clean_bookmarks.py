#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clean_bookmarks.py — 应用清理决策，产出清理后的书签 HTML + 《书签改动清单》

这是流水线的第 2 步（清理）。它**根据决策文件**对原始书签做机械处理：
  - 合并重复：merge_groups 中每组保留第一条，移除其余（相同网址只留一份）
  - 失效处置：dead_actions 中每条 url →
        "delete"        直接删除
        "move_to_review" 移入「待复查」文件夹（不删，留待本地最终确认）
        "keep"          保留不动
产出：
  - 新的书签 HTML（不覆盖原始输入）
  - 《书签改动清单.md》逐条记录所有 删除 / 合并 / 移动 操作，便于用户备查

决策文件由 AI agent 根据用户的选择生成；本脚本只负责机械执行。

用法（agent 调用）
------------------
  python3 clean_bookmarks.py \
      --input    bookmarks.html \
      --decisions decisions.json \
      --output   bookmarks_clean.html \
      [--changelog 书签改动清单.md]

decisions.json 格式
------------------
{
  "merge_groups": [            // 每组为重复 url 列表，保留第一项，移除其余
    ["https://x.com/a", "https://x.com/a"]
  ],
  "dead_actions": {            // url -> 动作
    "https://dead.example": "delete",
    "https://maybe.example": "move_to_review",
    "https://ok.example": "keep"
  },
  "review_folder": "待复查"     // 可选，默认「待复查」
}
"""
import argparse
import json
import re
import sys
from html.parser import HTMLParser
from collections import Counter


class BookmarkParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.folder_stack = []
        self.pending = None
        self.in_a = False
        self.a_text = ""
        self.a_href = None
        self.a_path = None
        self.bookmarks = []

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
            self.bookmarks.append({"href": self.a_href, "name": self.a_text.strip(),
                                   "path": self.a_path})
            self.in_a = False

    def handle_data(self, data):
        if self.pending is not None:
            self.pending += data
        elif self.in_a:
            self.a_text += data


def unescape(u):
    return u.replace("&amp;", "&").replace("&#38;", "&")


def main():
    ap = argparse.ArgumentParser(description="应用清理决策，产出清理后书签")
    ap.add_argument("--input", required=True)
    ap.add_argument("--decisions", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--changelog", default="书签改动清单.md")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as f:
        html = f.read()
    with open(args.decisions, encoding="utf-8") as f:
        dec = json.load(f)

    merge_groups = dec.get("merge_groups", [])
    dead_actions = dec.get("dead_actions", {})
    review_folder = dec.get("review_folder", "待复查")

    # 构建「需移除的 href 集合」与「需移入待复查的集合」
    remove_hrefs = set()
    move_hrefs = set()
    keep_hrefs = set()
    for grp in merge_groups:
        if len(grp) > 1:
            for u in grp[1:]:
                remove_hrefs.add(unescape(u))
    for u, act in dead_actions.items():
        uu = unescape(u)
        if act == "delete":
            remove_hrefs.add(uu)
        elif act == "move_to_review":
            move_hrefs.add(uu)
        elif act == "keep":
            keep_hrefs.add(uu)

    parser = BookmarkParser()
    parser.feed(html)
    bookmarks = parser.bookmarks

    # 统计每个 href 出现次数（处理同名重复在 HTML 中的出现）
    href_seen = Counter(unescape(b["href"]) for b in bookmarks)

    changes = []  # 改动清单行

    # 策略：
    # - remove_hrefs 中的 url：从 HTML 删除对应 <A> 标签
    # - move_hrefs 中的 url：把对应 <A> 移到「待复查」文件夹（在书签栏层级下新建）
    # 通过正则逐条处理 <A> 标签。

    pat = re.compile(r'<DT><A\b([^>]*?\bHREF=")([^"]*)(")([^>]*)>([^<]*)</A>')

    # 记录已处理（用于重复合并：第一组保留、其余删除）
    seen_for_merge = set()

    def repl(m):
        raw_href = m.group(2)
        href = raw_href.replace("&amp;", "&").replace("&#38;", "&")
        name = m.group(5).strip()
        # 合并：同一 url 第二次及以后出现 → 删除（保留首条）
        if href in {unescape(x) for grp in merge_groups for x in grp}:
            if href in seen_for_merge:
                return ""  # 移除重复
            seen_for_merge.add(href)
        if href in remove_hrefs:
            changes.append(("删除", name, href, "重复/失效，按决策移除"))
            return ""
        if href in move_hrefs:
            return ""  # 移入待复查（在末尾统一追加）
        return m.group(0)

    out_html = pat.sub(repl, html)

    # 统计合并移除数（重复）
    merged_removed = sum(1 for grp in merge_groups for u in grp[1:]) if merge_groups else 0

    # 追加「待复查」文件夹
    if move_hrefs:
        items = []
        for b in bookmarks:
            hu = unescape(b["href"])
            if hu in move_hrefs:
                items.append(f'        <DT><A HREF="{b["href"]}" ADD_DATE="{int(__import__("time").time())}">{b["name"]}</A>')
        block = (
            f'    <DT><H3>{review_folder}</H3>\n'
            f'    <DL><p>\n' + "\n".join(items) + "\n    </DL><p>\n"
        )
        # 插入到根级 <DL><p> 之后
        out_html = re.sub(r'(<DL><p>\s*)', lambda mm: mm.group(1) + "\n" + block, out_html, count=1)
        for b in bookmarks:
            if unescape(b["href"]) in move_hrefs:
                changes.append(("移入待复查", b["name"], unescape(b["href"]), "失效候选，留待本地确认"))

    with open(args.output, "w", encoding="utf-8") as f:
        f.write(out_html)

    # 改动清单
    lines = []
    lines.append("# 书签改动清单\n")
    lines.append(f"- 输入：`{args.input}`")
    lines.append(f"- 输出：`{args.output}`")
    lines.append(f"- 合并移除重复：{merged_removed} 条")
    lines.append(f"- 直接删除：{sum(1 for c in changes if c[0]=='删除')} 条")
    lines.append(f"- 移入「{review_folder}」：{sum(1 for c in changes if c[0]=='移入待复查')} 条\n")
    lines.append("| 操作 | 书签名称 | URL | 依据 |")
    lines.append("| --- | --- | --- | --- |")
    for op, name, href, reason in changes:
        lines.append(f"| {op} | {name} | {href} | {reason} |")
    with open(args.changelog, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    print(f"清理完成：输出 {args.output}")
    print(f"  合并移除重复 : {merged_removed}")
    print(f"  直接删除     : {sum(1 for c in changes if c[0]=='删除')}")
    print(f"  移入待复查   : {sum(1 for c in changes if c[0]=='移入待复查')}")
    print(f"  改动清单     : {args.changelog}")


if __name__ == "__main__":
    main()
