#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云函数版队列处理器（腾讯云 SCF / 阿里云 FC 通用）

背景: 混合架构中"本机定时抓取"的替代——国内云函数 IP 可抓微信公众号，
      服务器(NapCat, fetch_mode=queue)只入队 → Gitee 队列 → 本函数定时抓取入库，
      本机彻底解放（不用开机、不用计划任务）。

触发: 定时触发器，cron 每 10 分钟一次（阿里云 FC/腾讯云 SCF 均 7 段格式 秒 分 时 日 月 周 年）:
      0 */10 * * * * *   # ← */10 在"分"位；若写在首位"秒"位会变成每 10 秒触发！
限制: 函数超时建议 900s；每轮抓取上限 KB_MAX_PER_RUN（默认 3）防超时

环境变量（云函数控制台配置，凭据不写代码）:
    KB_GIT_URL      仓库地址。云端用 ssh: git@<git平台>:<org>/labknowledge.git
                    本地测试用 https: https://<git平台>/<org>/<repo>.git
    KB_SSH_KEY      部署公钥的私钥内容（仅 ssh 模式；含换行需原样粘贴）
    KB_GIT_USER     提交用户名，默认 bot
    KB_GIT_EMAIL    提交邮箱，默认 bot@lab-kb.local
    KB_MAX_PER_RUN  每轮抓取上限，默认 3
    KB_GAP_SEC      抓取间隔秒（防微信限流），默认 15
    KB_TIMEOUT_SEC  本轮总时长上限，默认 600

打包上传: cloud_queue_processor.py + wechat_fetcher.py + bs4 依赖(bs4, soupsieve)
          pip install beautifulsoup4 --target <zip_dir> 后与代码一并打包
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# ============ 常量 ============
FETCHER = Path(__file__).parent / "wechat_fetcher.py"   # 同目录，随 zip 分发
QUEUE_REL = "_queue/links.txt"
GIT_DIRS = ("_queue", "05_知识/文章收藏", "_attachments")

# 工作目录（云端 Linux 固定 /tmp；Windows 本机用 home 下目录，避开 8.3 短路径）
if os.name == "nt":
    WORK_ROOT = Path.home() / ".lab-kb-work"
else:
    WORK_ROOT = Path("/tmp/lab-kb-work")

WECHAT_URL_RE = re.compile(r"https?://mp\.weixin\.qq\.com/\S+")


def log(msg):
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def git(repo, args, env=None, timeout=120):
    r = subprocess.run(["git"] + args, cwd=repo, capture_output=True,
                       text=True, timeout=timeout, env=env)
    return r


def run():
    """一轮处理（云函数 handler 的实质）"""
    start = time.monotonic()
    gap = int(os.environ.get("KB_GAP_SEC", "15"))
    max_per_run = int(os.environ.get("KB_MAX_PER_RUN", "3"))
    timeout_sec = int(os.environ.get("KB_TIMEOUT_SEC", "600"))
    git_user = os.environ.get("KB_GIT_USER", "bot")
    git_email = os.environ.get("KB_GIT_EMAIL", "bot@lab-kb.local")
    repo_url = os.environ.get("KB_GIT_URL", "").strip()
    if not repo_url:
        log("缺少 KB_GIT_URL 环境变量，退出")
        return {"ok": False, "reason": "no KB_GIT_URL"}

    # 0. ssh 私钥写入（仅 ssh 模式）
    env = dict(os.environ)
    ssh_key = os.environ.get("KB_SSH_KEY", "").strip()
    if repo_url.startswith("git@"):
        ssh_dir = WORK_ROOT / ".ssh"
        ssh_dir.mkdir(parents=True, exist_ok=True)
        key = ssh_dir / "id_ed25519"
        key.write_text(ssh_key + "\n", encoding="utf-8")
        key.chmod(0o600)
        env["GIT_SSH_COMMAND"] = ("ssh -i " + str(key) +
                                  " -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null")

    # 1. clone 仓库（实例复用临时目录，先清后拉；仓库小，depth 1 秒级）
    WORK_ROOT.mkdir(parents=True, exist_ok=True)
    work = WORK_ROOT / "lab-kb"
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    r = git(WORK_ROOT, ["clone", "--depth", "1", "--single-branch",
                        "-b", "main", repo_url, str(work)], env=env, timeout=180)
    if r.returncode != 0:
        log(f"clone 失败: {r.stderr.strip()[-200:]}")
        return {"ok": False, "reason": "clone failed", "err": r.stderr[-200:]}
    log("clone 完成")
    git(work, ["config", "user.name", git_user])
    git(work, ["config", "user.email", git_email])

    # 2. 读队列
    qfile = work / QUEUE_REL
    if not qfile.exists():
        log("队列为空，结束")
        return {"ok": True, "pulled": 0, "failed": 0, "remaining": 0}
    urls = [u.strip() for u in qfile.read_text(encoding="utf-8").splitlines() if u.strip()]
    if not urls:
        log("队列为空，结束")
        return {"ok": True, "pulled": 0, "failed": 0, "remaining": 0}
    log(f"队列 {len(urls)} 条，本轮上限 {max_per_run} 条")

    # 3. 逐个抓取（每篇检查剩余时间，防超时；失败重试 1 次，访问限制可能是间歇的）
    done, failed = [], []
    for u in urls[:max_per_run]:
        if time.monotonic() - start > timeout_sec:
            log("已到时间上限，停止本轮")
            break
        ok = False
        for attempt in range(2):
            r = subprocess.run([sys.executable, str(FETCHER), u,
                                "--out", str(work / "05_知识" / "文章收藏"),
                                "--attachments", str(work / "_attachments"),
                                "--kb-root", str(work)],
                               capture_output=True, text=True, timeout=150,
                               encoding="utf-8", errors="replace")
            if r.returncode == 0:
                ok = True
                break
            msg = (r.stderr or r.stdout or "未知错误").strip().splitlines()[-1]
            log(f"  第 {attempt + 1} 次失败: {msg}")
            time.sleep(gap)
        if ok:
            done.append(u)
            saved = next((l for l in (r.stdout or "").splitlines() if l.startswith("已保存")), "")
            log(f"  ✅ {saved or u}")
        else:
            failed.append(u)
        time.sleep(gap)

    # 4. 更新队列 + 提交推送
    remain = [u for u in urls if u not in done]
    if remain:
        qfile.write_text("\n".join(remain) + "\n", encoding="utf-8")
    else:
        qfile.unlink(missing_ok=True)
    git(work, ["add", "-A"] + list(GIT_DIRS), env=env)
    r = git(work, ["commit", "-m",
                   f"bot: 云函数队列处理 {len(done)} 篇，失败 {len(failed)} 篇 "
                   f"{time.strftime('%Y-%m-%d %H:%M')}"], env=env)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        log(f"commit 失败: {r.stderr.strip()[-150:]}")
    p = git(work, ["push", "origin", "HEAD"], env=env, timeout=180)
    if p.returncode != 0:
        log(f"push 失败: {p.stderr.strip()[-150:]}")
    log(f"本轮完成: 成功 {len(done)} / 失败 {len(failed)} / 剩余 {len(remain)}"
        + ("" if p.returncode == 0 else "，push 失败"))
    return {"ok": p.returncode == 0, "pulled": len(done),
            "failed": len(failed), "remaining": len(remain)}


def handler(event, context):
    """云函数入口（腾讯云 SCF / 阿里云 FC 通用签名）"""
    log("===== 云函数队列处理开始 =====")
    try:
        return run()
    except Exception as e:
        log(f"异常: {e}")
        return {"ok": False, "reason": str(e)}


if __name__ == "__main__":
    # 本地测试: python cloud_queue_processor.py
    print(handler({}, None))
