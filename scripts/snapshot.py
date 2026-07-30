#!/usr/bin/env python3
"""Restore points for /fix-code --fix.

The obvious implementation of "let me undo those fixes" is wrong in ways that
silently lose work, so this is a script rather than a printed one-liner. What
the tempting version (`git stash create` + `git checkout <sha> -- .`) does,
verified empirically:

  * `git stash create` never captures untracked files, and untracked files are
    in scope for this skill — so a fix to a new file survives the "undo".
  * On a tree with only untracked changes it prints nothing and exits 0, so
    there is no restore point at all and nothing says so.
  * On a repo with no commits it errors outright.
  * `git checkout <sha> -- .` writes the **index** as well as the worktree, so
    on a partially staged file the staged version — which exists nowhere else —
    is destroyed. It also restores every tracked file, not just the ones edited.

So: snapshot each file individually as a blob, anchor the blobs against `gc`
behind a ref, and restore by path into the worktree only.

Usage:
  snapshot.py create <path>...     # returns the snapshot id on stdout
  snapshot.py undo [id]            # defaults to the most recent
  snapshot.py list
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

REF_PREFIX = "refs/fix-code/snap"


def git(*args: str, check: bool = True, env: dict | None = None) -> str:
    full = {**os.environ, **(env or {})}
    r = subprocess.run(
        ["git", *args], capture_output=True, text=True, errors="replace", env=full
    )
    if check and r.returncode != 0:
        raise SystemExit(f"git {' '.join(args)} failed: {r.stderr.strip()}")
    return r.stdout


def store_dir() -> str:
    git_dir = git("rev-parse", "--absolute-git-dir").strip()
    d = os.path.join(git_dir, "fix-code")
    os.makedirs(d, exist_ok=True)
    return d


def repo_root() -> str:
    return git("rev-parse", "--show-toplevel").strip()


def anchor(entries: list[dict], snap_id: str) -> str:
    """Make every blob reachable from a ref so `git gc` can't prune it.

    Uses a scratch index so nested paths work — `git mktree` would need the
    caller to build subtrees by hand.
    """
    idx = os.path.join(store_dir(), f"index-{snap_id}")
    env = {"GIT_INDEX_FILE": idx}
    try:
        git("read-tree", "--empty", env=env)
        for e in entries:
            if e["blob"] is None:
                continue
            git(
                "update-index", "--add", "--cacheinfo",
                f"100644,{e['blob']},{e['path']}", env=env,
            )
        tree = git("write-tree", env=env).strip()
        commit = git(
            "commit-tree", tree, "-m", f"fix-code restore point {snap_id}"
        ).strip()
        git("update-ref", f"{REF_PREFIX}/{snap_id}", commit)
        return commit
    finally:
        if os.path.exists(idx):
            os.remove(idx)


def create(paths: list[str]) -> int:
    if not paths:
        raise SystemExit("create needs at least one path")
    root = repo_root()
    snap_id = time.strftime("%Y%m%d-%H%M%S") + f"-{os.getpid()}"

    entries = []
    for p in paths:
        rel = os.path.relpath(os.path.abspath(p), root).replace(os.sep, "/")
        abs_p = os.path.join(root, rel)
        if os.path.exists(abs_p):
            blob = git("hash-object", "-w", "--", abs_p).strip()
            mode = os.stat(abs_p).st_mode & 0o777
            entries.append({"path": rel, "blob": blob, "existed": True,
                            "mode": mode})
        else:
            # The skill is about to create this file; undo means deleting it.
            entries.append({"path": rel, "blob": None, "existed": False,
                            "mode": None})

    commit = anchor(entries, snap_id)
    manifest = {
        "id": snap_id,
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "ref": f"{REF_PREFIX}/{snap_id}",
        "commit": commit,
        "entries": entries,
    }
    with open(os.path.join(store_dir(), f"{snap_id}.json"), "w") as fh:
        json.dump(manifest, fh, indent=2)

    print(snap_id)
    return 0


def manifests() -> list[dict]:
    d = store_dir()
    out = []
    for name in sorted(os.listdir(d)):
        if name.endswith(".json"):
            try:
                with open(os.path.join(d, name)) as fh:
                    out.append(json.load(fh))
            except (OSError, ValueError):
                continue
    return out


def undo(snap_id: str | None) -> int:
    all_m = manifests()
    if not all_m:
        raise SystemExit("no fix-code restore points in this repository")
    if snap_id:
        match = [m for m in all_m if m["id"] == snap_id]
        if not match:
            raise SystemExit(f"no restore point {snap_id}")
        m = match[0]
    else:
        m = all_m[-1]

    root = repo_root()
    restored, removed, missing = [], [], []
    for e in m["entries"]:
        abs_p = os.path.join(root, e["path"])
        if e["existed"]:
            blob = subprocess.run(
                ["git", "cat-file", "blob", e["blob"]],
                capture_output=True, check=False,
            )
            if blob.returncode != 0:
                missing.append(e["path"])
                continue
            os.makedirs(os.path.dirname(abs_p) or root, exist_ok=True)
            # Worktree only. Never touch the index: a partially staged file's
            # staged content exists nowhere else.
            with open(abs_p, "wb") as fh:
                fh.write(blob.stdout)
            if e["mode"]:
                os.chmod(abs_p, e["mode"])
            restored.append(e["path"])
        else:
            if os.path.exists(abs_p):
                os.remove(abs_p)
                removed.append(e["path"])

    print(f"restore point {m['id']} ({m['created']})")
    for p in restored:
        print(f"  restored  {p}")
    for p in removed:
        print(f"  removed   {p}  (did not exist before the fixes)")
    for p in missing:
        print(f"  FAILED    {p}  (blob gone — was the ref deleted?)")
    print("The index was not modified.")
    return 1 if missing else 0


def list_points() -> int:
    all_m = manifests()
    if not all_m:
        print("no restore points")
        return 0
    for m in all_m:
        paths = ", ".join(e["path"] for e in m["entries"][:3])
        extra = f" (+{len(m['entries']) - 3})" if len(m["entries"]) > 3 else ""
        print(f"{m['id']}  {m['created']}  {paths}{extra}")
    return 0


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "create":
        return create(sys.argv[2:])
    if cmd == "undo":
        return undo(sys.argv[2] if len(sys.argv) > 2 else None)
    if cmd == "list":
        return list_points()
    print(f"unknown command: {cmd}\n{__doc__}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
