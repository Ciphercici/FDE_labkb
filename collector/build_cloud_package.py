#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
打包云函数部署 zip（腾讯云 SCF / 阿里云 FC）

产物: collector/cloud_deploy/qq-collector-cloud.zip
内容: cloud_queue_processor.py + wechat_fetcher.py + bs4 纯 Python 依赖

用法: python build_cloud_package.py
"""
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
FETCHER_SRC = HERE / "wechat_fetcher.py"
OUT_DIR = HERE / "cloud_deploy"
DEPS_DIR = OUT_DIR / "deps"
ZIP_OUT = OUT_DIR / "qq-collector-cloud.zip"


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if DEPS_DIR.exists():
        shutil.rmtree(DEPS_DIR)
    DEPS_DIR.mkdir()

    # 1. 安装纯 Python 依赖（requests 全家 + bs4；打包冗余依赖，
    #    防止 FC/SCF 运行时未内置 requests 导致抓取器挂掉）
    #    charset_normalizer 3.x 默认带 mypyc 编译的 Windows .pyd → 强制 sdist 纯 Python 版
    print("安装 requests + beautifulsoup4 → deps/ ...")
    r = subprocess.run([sys.executable, "-m", "pip", "install",
                        "requests", "beautifulsoup4", "--target", str(DEPS_DIR),
                        "--no-binary", "charset_normalizer"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("pip 失败:", (r.stderr or r.stdout)[-300:])
        sys.exit(1)
    # 清理 pip 缓存残留 + 一切平台编译产物（.pyd/.so/.dll 上传 Linux 会 import 失败）
    for d in DEPS_DIR.iterdir():
        if d.name in ("bin",) or d.name.startswith("__pycache__"):
            shutil.rmtree(d, ignore_errors=True)
    for f in DEPS_DIR.rglob("*"):
        if f.is_file() and f.suffix.lower() in (".pyd", ".so", ".dll"):
            f.unlink()
            print("  排除平台二进制:", f.name)

    # 2. 组装 zip（入口文件在 zip 根，依赖在同级目录）
    with zipfile.ZipFile(ZIP_OUT, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(HERE / "cloud_queue_processor.py", "cloud_queue_processor.py")
        z.write(FETCHER_SRC, "wechat_fetcher.py")
        for f in sorted(DEPS_DIR.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(DEPS_DIR).as_posix())

    size = ZIP_OUT.stat().st_size / 1024
    print(f"打包完成: {ZIP_OUT}（{size:.0f} KB）")
    with zipfile.ZipFile(ZIP_OUT) as z:
        names = z.namelist()
    print("内容:", ", ".join(n.split('/')[0] for n in names if n.count('/') <= 1 and not n.endswith('/')))


if __name__ == "__main__":
    main()
