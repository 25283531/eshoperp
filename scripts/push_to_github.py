"""把本地分支推送到 GitHub —— 走 **Git Data API**（不是 git push）。

## 为什么需要这个脚本

本机对 `github.com` 的 HTTPS 存在 SNI 级拦截（TCP 能连通，但 TLS ClientHello 即被 RST），
`git push https://github.com/...` 必然失败；而 **`api.github.com` 是通的**。
Git Data API 允许我们直接构造 blob → tree → commit → ref，绕开 HTTPS git 协议。

## 传输层为什么用 curl 而不是 Python 的 requests/urllib

实测：本机 Python 自带 TLS 访问 `api.github.com` 同样可能被重置，而 **curl 稳定**。
因此这里统一用 `subprocess` 调 curl。

## 用法

    set GH_TOKEN=ghp_xxx                  # 或写入 %TEMP%\\ghtoken.txt
    python scripts/push_to_github.py

可选参数：
    --owner 25283531   --repo eshoperp   --branch main   --dry-run

## 自动适配两种远端状态

  - **空仓库**（无任何 commit）：创建根 commit + `POST /git/refs` 建 ref
  - **非空仓库**：以远端 HEAD 作为 parent，增量推送本地领先的 commit，再 `PATCH` ref

★ 用 SHA 去重：blob 内容未变则不重复上传，重跑是幂等的。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO_DIR = str(Path(__file__).resolve().parent.parent)
OWNER = "25283531"
REPO = "eshoperp"
BRANCH = "main"

_req_count = 0


# --------------------------------------------------------------------------- 基础


def git(args: list[str], binary: bool = False):
    r = subprocess.run(["git"] + args, cwd=REPO_DIR, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.decode('utf-8', 'replace')}")
    return r.stdout if binary else r.stdout.decode("utf-8")


def get_token() -> str:
    """token 优先取环境变量，其次取 %TEMP%\\ghtoken.txt。

    ★ 刻意**不**提供命令行参数传 token：那会让密钥落进 shell history。
    """
    tok = os.environ.get("GH_TOKEN", "").strip()
    if tok:
        return tok
    path = os.path.join(tempfile.gettempdir(), "ghtoken.txt")
    if os.path.exists(path):
        tok = open(path, encoding="utf-8").read().strip()
        if tok:
            return tok
    raise SystemExit(
        f"未找到 token。请二选一：\n"
        f"  1) set GH_TOKEN=ghp_xxx\n"
        f"  2) 把 token 单独一行写入 {path}\n"
        f"（不要通过命令行参数传，避免落进 shell history）"
    )


def api(method: str, pathname: str, body: dict | None = None, token: str = ""):
    """调 GitHub API，自带 5 次重试（覆盖传输失败 / 非 JSON / 限流）。"""
    global _req_count
    _req_count += 1
    args = [
        "curl", "-sS", "-X", method,
        "-H", f"Authorization: Bearer {token}",
        "-H", "Accept: application/vnd.github+json",
        "-H", "X-GitHub-Api-Version: 2022-11-28",
        "-H", "User-Agent: push-to-github",
        f"https://api.github.com{pathname}",
    ]
    tmp = None
    if body is not None:
        tmp = os.path.join(tempfile.gettempdir(), f"ghbody-{_req_count}-{os.getpid()}.json")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(body, f, ensure_ascii=False)
        args += ["-H", "Content-Type: application/json", "--data-binary", f"@{tmp}"]
    try:
        last = ""
        for attempt in range(1, 6):
            r = subprocess.run(args, capture_output=True)
            if r.returncode != 0:
                last = f"transport rc={r.returncode} err={r.stderr.decode('utf-8', 'replace')[:200]}"
                print(f"    retry {attempt}/5 ({last})")
                time.sleep(3)
                continue
            try:
                j = json.loads(r.stdout.decode("utf-8"))
            except Exception:
                last = r.stdout.decode("utf-8", "replace")[:200]
                print(f"    retry {attempt}/5 (non-json): {last}")
                time.sleep(3)
                continue
            if isinstance(j, dict) and j.get("message") and "sha" not in j:
                msg = str(j["message"])
                if "rate limit" in msg.lower() or "secondary" in msg.lower():
                    print(f"    retry {attempt}/5 (rate limit): {msg[:110]}")
                    time.sleep(10)
                    continue
                # 404 用于表示 ref 不存在，交由调用方处理
                return {"__error__": msg, "__status__": r.returncode}
            return j
        raise RuntimeError(f"API {method} {pathname} 连续 5 次失败：{last}")
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------------- 读取本地


def ls_tree(ref: str) -> list[dict]:
    raw = git(["ls-tree", "-r", "-z", ref])
    out = []
    for part in raw.split("\0"):
        if not part:
            continue
        tab = part.index("\t")
        mode, typ, sha = part[:tab].split(" ")
        out.append({"mode": mode, "type": typ, "sha": sha, "path": part[tab + 1:]})
    return out


def commit_message() -> str:
    return git(["log", "-1", "--pretty=%B"]).strip()


def person(field: str) -> dict:
    name = git(["config", "user.name"]).strip()
    email = git(["config", "user.email"]).strip()
    return {"name": name, "email": email}


# --------------------------------------------------------------------------- 主流程


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default=OWNER)
    ap.add_argument("--repo", default=REPO)
    ap.add_argument("--branch", default=BRANCH)
    ap.add_argument("--dry-run", action="store_true", help="只列出将要上传的文件，不调 API")
    args = ap.parse_args()

    base = f"/repos/{args.owner}/{args.repo}"

    local_head = git(["rev-parse", args.branch]).strip()
    entries = ls_tree(local_head)

    print(f"本地 {args.branch} = {local_head[:8]}，共 {len(entries)} 个文件")

    non_blob = [e for e in entries if e["type"] != "blob"]
    if non_blob:
        print(f"!! 含 {len(non_blob)} 个非 blob 条目（子模块/软链），Git Data API 无法上传：")
        for e in non_blob[:10]:
            print(f"   {e['type']} {e['path']}")
        return 2

    if args.dry_run:
        for e in entries[:40]:
            print(f"   {e['mode']} {e['path']}")
        if len(entries) > 40:
            print(f"   ... 其余 {len(entries) - 40} 个省略")
        total = sum(len(git(["cat-file", "blob", e["sha"]], binary=True)) for e in entries)
        print(f"dry-run 完成：{len(entries)} 个文件，合计 {total / 1024:.0f} KiB")
        return 0

    # dry-run 之后才取 token —— 本地检查不该因为没有凭证就做不了
    token = get_token()

    # 探测远端是否已存在该分支
    ref = api("GET", f"{base}/git/ref/heads/{args.branch}", token=token)
    if ref.get("__error__"):
        remote_sha = None
        print("远端分支不存在 → 按**空仓库**处理（创建根 commit）")
    else:
        remote_sha = ref["object"]["sha"]
        print(f"远端 {args.branch} = {remote_sha[:8]} → 增量推送")

    # 远端已有的 blob 不必重传
    known: set[str] = set()
    if remote_sha:
        remote_tree = api("GET", f"{base}/git/trees/{remote_sha}?recursive=1", token=token)
        for item in remote_tree.get("tree", []):
            if item["type"] == "blob":
                known.add(item["sha"])

    tree_items: list[dict] = []
    uploaded = 0
    for i, e in enumerate(entries, 1):
        sha = e["sha"]
        if sha not in known:
            raw = git(["cat-file", "blob", sha], binary=True)
            blob = api(
                "POST", f"{base}/git/blobs",
                {"content": base64.b64encode(raw).decode("ascii"), "encoding": "base64"},
                token=token,
            )
            if blob.get("__error__"):
                raise RuntimeError(f"上传 blob 失败 {e['path']}: {blob['__error__']}")
            sha = blob["sha"]
            uploaded += 1
        tree_items.append({"path": e["path"], "mode": e["mode"], "type": "blob", "sha": sha})
        if i % 50 == 0 or i == len(entries):
            print(f"   {i}/{len(entries)} 文件已处理（新上传 blob {uploaded}）")

    tree = api("POST", f"{base}/git/trees", {"tree": tree_items}, token=token)
    if tree.get("__error__"):
        raise RuntimeError(f"创建 tree 失败: {tree['__error__']}")
    print(f"tree {tree['sha'][:8]} ({len(tree_items)} 项)")

    parents = [remote_sha] if remote_sha else []
    who = person("author")
    commit = api(
        "POST", f"{base}/git/commits",
        {"message": commit_message(), "tree": tree["sha"], "parents": parents,
         "author": who, "committer": who},
        token=token,
    )
    if commit.get("__error__"):
        raise RuntimeError(f"创建 commit 失败: {commit['__error__']}")
    print(f"commit {commit['sha'][:8]}")

    if remote_sha:
        updated = api("PATCH", f"{base}/git/refs/heads/{args.branch}",
                      {"sha": commit["sha"]}, token=token)
    else:
        updated = api("POST", f"{base}/git/refs",
                      {"ref": f"refs/heads/{args.branch}", "sha": commit["sha"]}, token=token)
    if updated.get("__error__"):
        raise RuntimeError(f"更新 ref 失败: {updated['__error__']}")

    print(f"\n✅ 推送完成：{args.owner}/{args.repo} 的 {args.branch} -> {commit['sha'][:8]}")
    print(f"   https://github.com/{args.owner}/{args.repo}")
    print(f"   共 {_req_count} 次 API 调用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
