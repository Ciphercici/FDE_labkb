#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本机队列处理器（混合架构处理端）

背景: 服务器（海外 IP）收 QQ 链接但抓不了微信（海外 IP 被微信风控），
      本机（国内 IP）能抓。本机开机时运行本脚本，把服务器排队的链接逐个抓取入库。

流程: git pull → 读 _queue/links.txt → 逐个抓取（wechat_fetcher）→ 更新队列 → git push

用法:
    python queue_processor.py                  # 处理一轮（供计划任务调用）
    python queue_processor.py --once-verbose   # 处理一轮并打印全部输出

计划任务（Windows）:
    schtasks /Create /TN "QQ知识库队列处理" /TR "python queue_processor.py" /SC MINUTE /MO 10 /F

依赖: pip install requests beautifulsoup4  (与抓取器相同)
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

# ============ 配置 ============
KB_ROOT = Path(r"<kb_root>")
FETCHER = KB_ROOT / "_tools" / "wechat_fetcher" / "wechat_fetcher.py"
QUEUE_FILE = KB_ROOT / "_queue" / "links.txt"
OUT_DIR = KB_ROOT / "05_知识" / "文章收藏"
ATT_DIR = KB_ROOT / "_attachments"
GAP_SEC = 15          # 每篇抓取间隔（防微信限流）
RETRY = 1             # 失败重试次数
# ===============================

LOG_FILE = KB_ROOT / "_tools" / "qq_collector" / "queue_processor.log"


def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def git(args, check=False):
    r = subprocess.run(["git"] + args, cwd=KB_ROOT,
                       capture_output=True, text=True, timeout=60)
    if check and r.returncode != 0:
        log(f"git {' '.join(args)} 失败: {r.stderr.strip()[-120:]}")
    return r


def fetch_one(url):
    """调用抓取器；返回 (ok, 摘要)"""
    cmd = [sys.executable, str(FETCHER), url,
           "--out", str(OUT_DIR), "--attachments", str(ATT_DIR),
           "--kb-root", str(KB_ROOT)]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        return False, (r.stderr or r.stdout or "未知错误").strip().splitlines()[-1]
    lines = (r.stdout or "").strip().splitlines()
    saved = next((l for l in lines if l.startswith("已保存")), "")
    stats = next((l for l in lines if l.startswith("图片")), "")
    return True, f"{saved} {stats}".strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once-verbose", action="store_true")
    args = ap.parse_args()
    if not args.once_verbose:
        log("===== 队列处理开始 =====")

    # 1. 同步队列
    git(["pull", "origin", "main"])
    if not QUEUE_FILE.exists():
        log("队列为空，结束")
        return
    urls = [u.strip() for u in QUEUE_FILE.read_text(encoding="utf-8").splitlines() if u.strip()]
    if not urls:
        log("队列为空，结束")
        return
    log(f"队列 {len(urls)} 条，开始处理")

    # 2. 逐个抓取（失败重试，重试后仍失败则放弃该链接并记录）
    done, failed = [], []
    for u in urls:
        ok, summary = False, ""
        for attempt in range(RETRY + 1):
            ok, summary = fetch_one(u)
            if ok:
                break
            log(f"  尝试 {attempt + 1} 失败: {summary}")
            time.sleep(GAP_SEC)
        if ok:
            done.append(u)
            log(f"  ✅ {summary}")
        else:
            failed.append(u)
            log(f"  ❌ 放弃: {u} ({summary})")
        time.sleep(GAP_SEC)

    # 3. 更新队列（已处理移除）+ 提交推送
    remain = [u for u in urls if u not in done]
    if remain:
        QUEUE_FILE.write_text("\n".join(remain) + ("\n" if remain else ""), encoding="utf-8")
    else:
        QUEUE_FILE.unlink(missing_ok=True)   # 队列清空则删文件
    git(["add", "-A", "_queue", "05_知识/文章收藏", "_attachments"])
    git(["commit", "-m", f"bot: 队列处理 {len(done)} 篇，失败 {len(failed)} 篇 "
                         f"{time.strftime('%Y-%m-%d %H:%M')}"])
    p = git(["push", "origin", "main"])
    log(f"本轮完成: 成功 {len(done)} / 失败 {len(failed)} / 剩余 {len(remain)}"
        + ("，push 失败" if p.returncode != 0 else ""))


if __name__ == "__main__":
    main()
