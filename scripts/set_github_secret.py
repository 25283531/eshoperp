"""设置 GitHub Actions Secret（经 REST API）。

## 为什么需要这个脚本

本机没有装 `gh` CLI，而 Secret 的值必须先用**仓库公钥**做 libsodium 密封加密
（`SealedBox`）才能 PUT 上去，没法直接传明文。

依赖：`pip install pynacl`

## 用法

    # Windows Git Bash / PowerShell 均可；值从环境变量读，不落 shell history
    GH_TOKEN=ghp_xxx SECRET_VALUE='要写入的值' python scripts/set_github_secret.py VITE_ADMIN_TOKEN

★ 值**只**从环境变量读取，不接受命令行参数 —— 命令行会落进 shell history。

## 可选参数

    --owner 25283531   --repo eshoperp
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import tempfile
import time


def get_token() -> str:
    tok = os.environ.get("GH_TOKEN", "").strip()
    if not tok:
        raise SystemExit("请先设置环境变量 GH_TOKEN")
    return tok


def api(method: str, pathname: str, token: str, body: dict | None = None) -> dict:
    args = [
        "curl", "-sS", "-X", method,
        "-H", f"Authorization: Bearer {token}",
        "-H", "Accept: application/vnd.github+json",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
        "-H", "User-Agent: set-github-secret",
        f"https://api.github.com{pathname}",
    ]
    tmp = None
    if body is not None:
        tmp = os.path.join(tempfile.gettempdir(), f"ghsec-{os.getpid()}.json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
        args += ["-H", "Content-Type: application/json", "--data-binary", f"@{tmp}"]
    try:
        for attempt in range(1, 4):
            r = subprocess.run(args, capture_output=True)
            if r.returncode != 0:
                print(f"    retry {attempt}/3 (transport): {r.stderr.decode('utf-8', 'replace')[:160]}")
                time.sleep(3)
                continue
            try:
                return json.loads(r.stdout.decode("utf-8"))
            except Exception:
                print(f"    retry {attempt}/3 (non-json)")
                time.sleep(3)
        raise RuntimeError(f"API {method} {pathname} 连续 3 次失败")
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def encrypt(public_key_b64: str, secret_value: str) -> str:
    """用仓库公钥做 libsodium 密封加密。"""
    from nacl import encoding, public  # 延迟导入：仅真正需要时才依赖 pynacl

    pub = public.PublicKey(public_key_b64.encode("ascii"), encoding.Base64Encoder())
    sealed = public.SealedBox(pub).encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(sealed).decode("ascii")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("secret_name", help="Secret 名，如 VITE_ADMIN_TOKEN")
    ap.add_argument("--owner", default="25283531")
    ap.add_argument("--repo", default="eshoperp")
    args = ap.parse_args()

    token = get_token()
    secret_value = os.environ.get("SECRET_VALUE")
    if not secret_value:
        raise SystemExit("请先设置环境变量 SECRET_VALUE 为要写入的值")

    base = f"/repos/{args.owner}/{args.repo}"

    pk = api("GET", f"{base}/actions/secrets/public-key", token)
    if "key" not in pk:
        raise SystemExit(f"获取公钥失败：{pk}")
    print(f"已获取仓库公钥 key_id={pk.get('key_id')}")

    r = api(
        "PUT", f"{base}/actions/secrets/{args.secret_name}",
        token,
        {"encrypted_value": encrypt(pk["key"], secret_value), "key_id": pk["key_id"]},
    )
    if isinstance(r, dict) and r.get("message"):
        raise SystemExit(f"设置失败：{r['message']}")

    # 列出已有 Secret 确认真写进去了（只列名字，不回显值）
    listed = api("GET", f"{base}/actions/secrets", token)
    names = [s["name"] for s in listed.get("secrets", [])] if isinstance(listed, dict) else []
    print(f"✅ Secret `{args.secret_name}` 已设置")
    print(f"   当前仓库 Secrets：{names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
