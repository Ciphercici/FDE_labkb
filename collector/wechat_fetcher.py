#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信公众号文章抓取器（实验室知识库自动收藏用）

用法:
    python wechat_fetcher.py "https://mp.weixin.qq.com/s/xxx"
    python wechat_fetcher.py "https://mp.weixin.qq.com/s/xxx" --out ../05_知识/文章收藏 --attachments ../_attachments

图片"三件套"（解决公众号图片裁剪不顺畅）:
    1. 防盗链  - 图片请求必须带 Referer: https://mp.weixin.qq.com/
    2. 懒加载  - 正文 img 的 src 是灰色占位，真实地址在 data-src
    3. CDN格式 - webp/jpg 直接本地化下载，Obsidian 均可显示，离线可看

输出:
    <out>/<YYYY-MM-DD>_<标题>.md         （frontmatter + 正文 markdown）
    <attachments>/<标题>/imgN.<ext>      （本地化图片，相对路径引用）

依赖: pip install requests beautifulsoup4
"""

import argparse
import os
import re
import sys
from datetime import date
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
# 完整浏览器指纹：模拟真实浏览器请求特征，降低被访问限制识别的概率
HEADERS = {
    "User-Agent": UA,
    "Referer": "https://mp.weixin.qq.com/",
    "Accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

IMAGE_TYPES = {
    "png": ".png", "jpg": ".jpg", "jpeg": ".jpg",
    "gif": ".gif", "webp": ".webp", "bmp": ".bmp",
}
SKIP_MEDIA_CLASS = re.compile(
    r"mp-common-videoslot|mp-common-mpvoice|mp-common-mpaudio|"
    r"js_article_video_area|js_article_audio_area|js_article_video",
    re.I,
)
ILLEGAL_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')
MULTI_SPACE = re.compile(r"[ \t]{2,}")


def clean_name(s, maxlen=60):
    """清洗成可做文件名的字符串：非法字符和空白 → _（文件名不含空格，Obsidian 链接才稳定）"""
    s = ILLEGAL_CHARS.sub("_", s)
    s = MULTI_SPACE.sub(" ", s)
    s = re.sub(r"\s+", "_", s)
    return s.strip("._")[:maxlen]


def fetch_html(url):
    r = requests.get(url, headers=HEADERS, timeout=20)
    r.raise_for_status()
    # 微信页面一律 UTF-8，直接指定；不要用 apparent_encoding——
    # 纯 Python 版 charset_normalizer（云函数 --no-binary 安装）会把中文
    # 误判成其他编码，导致全文乱码（2026-08-11 云函数实测踩坑）
    r.encoding = "utf-8"
    return r.text


def parse_meta(soup):
    """标题 / 作者 / 发布时间"""

    def og(prop):
        m = soup.find("meta", attrs={"property": prop})
        return m.get("content", "").strip() if m else ""

    title = og("og:title")
    if not title:
        h = soup.find("h1", id="activity-name")
        title = h.get_text(strip=True) if h else ""

    author = og("og:article:author")
    if not author:
        m = soup.find("meta", attrs={"name": "author"})
        author = m.get("content", "").strip() if m else ""

    pub = og("og:article:published_time")
    if not pub:
        e = soup.find("em", id="publish_time")
        pub = e.get_text(strip=True) if e else ""
    return title, author, pub


BLOCK_TAGS = (
    "p", "h1", "h2", "h3", "h4", "h5", "h6",
    "ul", "ol", "li", "table", "pre", "blockquote",
    "div", "section", "article", "figure",
)


class ArticleBuilder:
    """正文容器 → markdown 块；图片 → 本地下载 + 相对路径引用"""

    def __init__(self, slug, att_dir, att_rel):
        self.slug = slug
        self.att_dir = att_dir
        # att_rel: 附件目录相对文章输出目录的相对路径（如 ../../_attachments/标题）
        self.att_rel = att_rel
        self.img_count = 0
        self.fail_count = 0
        self.att_dir.mkdir(parents=True, exist_ok=True)

    # ---------- 图片 ----------

    def image(self, img):
        url = (img.get("data-src") or img.get("src") or "").strip()
        if not url or url.startswith(("about:", "javascript:", "data:")):
            self.fail_count += 1
            return "[图片下载失败]"
        if "res.wx.qq.com" in url and "mmbiz" not in url:
            return ""  # 微信默认 loading 占位图，忽略
        seq = self.img_count + 1
        rel = self.download(url, self._ext(img, url), seq)
        if rel is None:
            self.fail_count += 1
            return "[图片下载失败]"
        self.img_count = seq
        return f"![]({rel})"

    def _ext(self, img, url):
        """扩展名推断顺序：wx_fmt 参数 → data-type 属性 → URL 后缀 → 兜底 .jpg"""
        q = parse_qs(urlparse(url).query)
        if "wx_fmt" in q and q["wx_fmt"][0].lower() in IMAGE_TYPES:
            return IMAGE_TYPES[q["wx_fmt"][0].lower()]
        t = (img.get("data-type") or "").lower()
        if t in IMAGE_TYPES:
            return IMAGE_TYPES[t]
        e = Path(urlparse(url).path).suffix.lower()
        if e in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp"):
            return ".jpg" if e == ".jpeg" else e
        return ".jpg"

    def download(self, url, ext, seq):
        try:
            r = requests.get(url, headers=HEADERS, timeout=20)
            if r.status_code != 200 or not r.content:
                return None
            ctype = r.headers.get("Content-Type", "")
            if "image/svg" in ctype and ext != ".gif":
                ext = ".svg"
            elif "image/png" in ctype and ext not in (".png", ".gif"):
                ext = ".png"
            elif "image/webp" in ctype and ext not in (".png", ".gif"):
                ext = ".webp"
            name = f"img{seq}{ext}"
            (self.att_dir / name).write_bytes(r.content)
            return f"{self.att_rel}/{name}"
        except requests.RequestException:
            return None

    # ---------- 文本 ----------

    def inline(self, node):
        """行内元素 → markdown 行内文本"""
        parts = []
        for child in node.children:
            if isinstance(child, NavigableString):
                # HTML 空白折叠：文本节点内的换行/多空格 → 单个空格（只有 <br> 才是显式换行）
                parts.append(re.sub(r"\s+", " ", str(child)))
            elif isinstance(child, Tag):
                name = child.name.lower()
                if name == "br":
                    parts.append("\n")
                elif name in ("strong", "b"):
                    parts.append("**" + self.inline(child) + "**")
                elif name in ("em", "i"):
                    parts.append("*" + self.inline(child) + "*")
                elif name == "code":
                    parts.append("`" + child.get_text() + "`")
                elif name == "a":
                    txt = self.inline(child) or child.get_text(strip=True)
                    href = (child.get("href") or "").strip()
                    if href.startswith("http"):
                        parts.append(f"[{txt}]({href})")
                    else:
                        parts.append(txt)
                elif name in ("img", "image"):
                    parts.append(self.image(child))
                else:
                    parts.append(self.inline(child))
        return "".join(parts)

    # ---------- 代码块 ----------

    def code_block(self, node):
        """pre/code → 代码文本。
        微信代码块结构：pre > code > span.code-snippet__line（一行一个 span，行间无换行符）。
        按行级元素拆分补 \n；高亮 span 在行内合并。"""
        lines = []
        # 微信格式：所有行 span（跨层查找）
        line_spans = node.select("span.code-snippet__line")
        if line_spans:
            for sp in line_spans:
                t = sp.get_text("", strip=False)
                if t.strip():
                    lines.append(t.rstrip("\n"))
        else:
            # 通用 fallback：最内层代码容器（code 或 pre）的直接子元素按行处理
            target = node.find("code") or node
            for child in target.children:
                if isinstance(child, NavigableString):
                    if str(child).strip():
                        lines.append(str(child).rstrip("\n"))
                elif isinstance(child, Tag):
                    if child.name == "br":
                        lines.append("")
                    else:
                        lines.append(child.get_text().rstrip("\n"))
        # 微信排版产物：每行代码后跟一个空行 span → 删掉被代码行夹住的孤立空行；
        # 真实连续空行（作者故意留白）压缩为一个
        cleaned = []
        i, n = 0, len(lines)
        while i < n:
            if lines[i].strip():
                cleaned.append(lines[i])
                i += 1
            else:
                j = i
                while j < n and not lines[j].strip():
                    j += 1
                block_len = j - i
                if block_len >= 2 or not (i > 0 and j < n):
                    cleaned.append("")
                i = j
        text = "\n".join(cleaned)
        text = re.sub(r"[ \t]+\n", "\n", text)      # 去行尾空白
        return text

    # ---------- 表格 ----------

    def table(self, table):
        """table → 二维数组。首行含 th 作表头；无 th 时首行当表头（markdown 表格必需）"""
        rows = []
        for tr in table.find_all("tr"):
            cells = []
            for cell in tr.find_all(["th", "td"]):
                t = re.sub(r"\s+", " ", self.inline(cell)).strip()
                cells.append(t)
            if cells:
                rows.append(cells)
        if not rows:
            return None
        ncols = max(len(r) for r in rows)
        rows = [r + [""] * (ncols - len(r)) for r in rows]  # 补齐列数
        return rows

    # ---------- 块 ----------

    def is_inline_container(self, node):
        """容器内只有文本和行内标签（code/b/strong/em/span/img...）→ 整体按段落内联渲染。
        微信编辑器常把段落渲染成 div + 混合行内子节点，逐个拆开会断裂语义。"""
        return not any(
            isinstance(c, Tag) and c.name.lower() in BLOCK_TAGS
            for c in node.children
        )

    def paragraph(self, node):
        """行内混合容器 → 段落文本（换行压平，br 保留为硬换行）"""
        txt = self.inline(node)
        txt = re.sub(r"\n{2,}", "\n", txt).strip()
        return txt if txt else None

    def walk(self, container, out):
        """容器子节点 → [(类型, 内容), ...]"""
        for child in container.children:
            if isinstance(child, NavigableString):
                txt = re.sub(r"\s+", " ", str(child)).strip()
                if txt:
                    out.append(("p", txt))
                continue
            if not isinstance(child, Tag):
                continue
            name = child.name.lower()
            if name in ("script", "style", "iframe"):
                continue
            if name == "br":
                continue
            cls = " ".join(child.get("class", [])) if child.get("class") else ""
            if SKIP_MEDIA_CLASS.search(cls):
                out.append(("p", "[视频/音频，未保存]"))
                continue
            if name == "p":
                txt = self.inline(child)
                txt = re.sub(r"\n{2,}", "\n", txt).strip()
                if txt:
                    out.append(("p", txt))
            elif name in ("h1", "h2", "h3", "h4", "h5", "h6"):
                t = child.get_text(strip=True)
                if t:
                    out.append((name, t))
            elif name == "blockquote":
                inner = []
                self.walk(child, inner)
                if inner:
                    out.append(("blockquote", inner))
            elif name in ("ul", "ol"):
                items = []
                for li in child.find_all("li", recursive=False):
                    t = self.inline(li).strip()
                    if t:
                        items.append(t)
                if items:
                    out.append((name, items))
            elif name in ("img", "image"):
                img_md = self.image(child)
                if img_md:
                    out.append(("img", img_md))
            elif name == "pre":
                out.append(("code", self.code_block(child).strip("\n")))
            elif name == "table":
                t = self.table(child)
                if t:
                    out.append(("table", t))
            elif name in ("div", "section", "article", "figure"):
                if self.is_inline_container(child):
                    txt = self.paragraph(child)
                    if txt:
                        out.append(("p", txt))
                else:
                    self.walk(child, out)  # 含块级元素才递归展开
            elif name in ("figcaption", "li", "td", "th", "span"):
                txt = self.paragraph(child)
                if txt:
                    out.append(("p", txt))
            else:
                self.walk(child, out)
        return out


def render(blocks):
    """块列表 → markdown 字符串"""
    lines = []
    for kind, val in blocks:
        if kind == "p":
            # 段内 br 换行 → markdown 硬换行（行尾两空格）
            lines.append("\n  ".join(val.split("\n")))
        elif kind.startswith("h"):
            lines.append("#" * int(kind[1]) + " " + val)
        elif kind == "blockquote":
            inner = render(val)
            lines.append("\n".join(("> " + l if l else ">") for l in inner.splitlines()))
        elif kind == "ul":
            lines.extend(f"- {t}" for t in val)
        elif kind == "ol":
            lines.extend(f"{i}. {t}" for i, t in enumerate(val, 1))
        elif kind == "img":
            lines.append(val)
        elif kind == "code":
            lines.append("```\n" + val + "\n```")
        elif kind == "table":
            rows = val
            lines.append("| " + " | ".join(rows[0]) + " |")
            lines.append("| " + " | ".join(["---"] * len(rows[0])) + " |")
            for row in rows[1:]:
                lines.append("| " + " | ".join(row) + " |")
        lines.append("")
    return "\n".join(lines).strip()


def save_article(url, title, author, pub, body_md, out_dir, slug):
    today = date.today().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    p = out_dir / f"{today}_{slug}.md"
    n = 2
    while p.exists():
        p = out_dir / f"{today}_{slug}-{n}.md"
        n += 1
    safe_title = title.replace('"', "'")
    fm = (
        "---\n"
        f'title: "{safe_title}"\n'
        'type: knowledge\n'
        'source: "微信公众号"\n'
        f'url: "{url}"\n'
        f'author: "{author}"\n'
        f'published: "{pub}"\n'
        f'saved: "{today}"\n'
        "tags: []\n"
        "status: active\n"
        "access_level: public\n"
        "---\n\n"
    )
    p.write_text(fm + body_md + "\n", encoding="utf-8")
    return p


def main():
    ap = argparse.ArgumentParser(description="微信公众号文章抓取器")
    ap.add_argument("url", help="公众号文章链接（mp.weixin.qq.com/s/...）")
    ap.add_argument("--out", default="05_知识/文章收藏", help="markdown 输出目录（默认 05_知识/文章收藏）")
    ap.add_argument("--attachments", default="_attachments", help="附件根目录（默认 _attachments）")
    ap.add_argument("--kb-root", default=".", help="知识库(vault)根目录，图片用根相对路径引用（默认当前目录）")
    ap.add_argument("--stdout", action="store_true", help="只输出 markdown 到终端，不落盘（调试用）")
    args = ap.parse_args()

    host = urlparse(args.url).netloc.lower()
    if host not in ("mp.weixin.qq.com", "weixin.qq.com"):
        sys.exit("仅支持 mp.weixin.qq.com 链接")

    try:
        html = fetch_html(args.url)
    except requests.RequestException as e:
        sys.exit(f"抓取页面失败: {e}")

    soup = BeautifulSoup(html, "html.parser")
    title, author, pub = parse_meta(soup)
    if not title:
        reason = "链接失效 / 需要登录 / 触发访问限制"
        for marker in (
            "该内容已被发布者删除",
            "此内容因违规无法查看",
            "此内容被投诉且经审核涉嫌侵权",
            "此内容涉嫌违反相关法律法规",
            "参数错误",
        ):
            if marker in html:
                reason = marker
                break
        sys.exit(f"抓取失败：{reason}，请人工打开链接确认")

    slug = clean_name(title)
    content = soup.find("div", id="js_content") or soup.find("div", class_="rich_media_content")
    if content is None:
        sys.exit("未找到正文容器 #js_content（可能是图文卡片或转发页）")

    out_dir = Path(args.out)
    kb_root = Path(args.kb_root)
    att_dir = Path(args.attachments) / slug
    # 用 vault 根相对路径（不带 ../）：Obsidian 对根相对路径解析最稳定，中文/空格文件名也兼容
    att_rel = os.path.relpath(att_dir, kb_root).replace("\\", "/")
    builder = ArticleBuilder(slug, att_dir, att_rel)
    body = render(builder.walk(content, []))
    if args.stdout:
        print(body)
        return
    path = save_article(args.url, title, author, pub, body, Path(args.out), slug)
    print(f"已保存: {path}")
    print(f"标题: {title} | 作者: {author or '未知'} | 发布: {pub or '未知'}")
    print(f"图片: {builder.img_count} 成功 / {builder.fail_count} 失败")


if __name__ == "__main__":
    main()
