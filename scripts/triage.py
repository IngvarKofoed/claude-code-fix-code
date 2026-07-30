#!/usr/bin/env python3
"""Deterministic scope resolution and tier selection for /fix-code.

Everything here is a mechanical function of `git diff --numstat` plus filenames,
so it runs identically on every invocation. The point is that the announced tier
is reproducible: a tier that depends on how a model felt about the diff can't be
sanity-checked by the user.

Emits JSON on stdout. Judgment-requiring signals are kept in a separate
`judgment_candidates` field so it's obvious which part was computed and which
part still needs a reader.

Usage:
  triage.py                       # auto: working tree, else branch-vs-base
  triage.py --range HEAD~3..HEAD  # explicit range
  triage.py --branch              # force branch mode even on a dirty tree
  triage.py --paths src/ lib/x.ts # restrict to paths
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import subprocess
import sys

# ---------------------------------------------------------------- git plumbing


def git(*args: str, check: bool = True) -> str:
    r = subprocess.run(
        ["git", *args], capture_output=True, text=True, errors="replace"
    )
    if check and r.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {r.stderr.strip()}")
    return r.stdout


class GitError(Exception):
    pass


def in_repo() -> bool:
    try:
        return git("rev-parse", "--git-dir").strip() != ""
    except GitError:
        return False


def has_commits() -> bool:
    try:
        git("rev-parse", "--verify", "HEAD")
        return True
    except GitError:
        return False


def is_dirty() -> bool:
    return git("status", "--porcelain").strip() != ""


def default_branch() -> tuple[str | None, str]:
    """Return (ref, how_found). Never guesses silently."""
    out = git("symbolic-ref", "refs/remotes/origin/HEAD", check=False).strip()
    if out:
        return out.replace("refs/remotes/", ""), "origin/HEAD"

    out = git("remote", "show", "origin", check=False)
    for line in out.splitlines():
        if "HEAD branch:" in line:
            name = line.split(":", 1)[1].strip()
            if name and name != "(unknown)":
                ref = f"origin/{name}"
                if git("rev-parse", "--verify", ref, check=False).strip():
                    return ref, "git remote show origin"

    for name in ("main", "master", "trunk"):
        for ref in (f"origin/{name}", name):
            if git("rev-parse", "--verify", ref, check=False).strip():
                return ref, f"first existing of main/master/trunk ({ref})"

    return None, "not found — ask the user"


# ------------------------------------------------------------ noise exclusion

LOCKFILES = {
    "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml",
    "bun.lockb", "Cargo.lock", "poetry.lock", "Pipfile.lock", "uv.lock",
    "go.sum", "composer.lock", "Gemfile.lock", "pubspec.lock",
    "packages.lock.json", "mix.lock", "flake.lock",
}

GENERATED_GLOBS = (
    "*.pb.go", "*_pb2.py", "*_pb2_grpc.py", "*_pb.js", "*.pb.cc", "*.pb.h",
    "*.g.dart", "*.freezed.dart", "*.g.cs", "*.designer.cs",
    "*.generated.*", "*_generated.*", "*.min.js", "*.min.css", "*.map",
    "*.snap", "*.golden",
)

VENDOR_DIRS = (
    "node_modules", "vendor", "third_party", "thirdparty", ".venv", "venv",
    "dist", "build", "target", "__snapshots__", ".next", ".nuxt", "coverage",
    "Pods", "bower_components",
)

TEST_GLOBS = (
    "*_test.go", "*_test.py", "test_*.py", "*.test.*", "*.spec.*",
    "*Test.java", "*Tests.cs", "*_spec.rb", "*_test.rb", "*_test.exs",
)
TEST_DIRS = ("test", "tests", "__tests__", "spec", "specs", "e2e", "testdata")


def path_parts(p: str) -> list[str]:
    return p.replace("\\", "/").split("/")


def is_vendored(p: str) -> bool:
    return any(part in VENDOR_DIRS for part in path_parts(p))


def is_test(p: str) -> bool:
    parts = path_parts(p)
    base = parts[-1]
    if any(part in TEST_DIRS for part in parts[:-1]):
        return True
    return any(fnmatch.fnmatch(base, g) for g in TEST_GLOBS)


def linguist_generated(paths: list[str]) -> set[str]:
    """Honor .gitattributes linguist-generated markers."""
    if not paths:
        return set()
    inp = "\n".join(paths)
    out = subprocess.run(
        ["git", "check-attr", "--stdin", "linguist-generated"],
        input=inp, capture_output=True, text=True, errors="replace",
    ).stdout
    marked = set()
    for line in out.splitlines():
        # format: <path>: linguist-generated: set|true|unspecified
        if line.rstrip().endswith((": set", ": true")):
            marked.add(line.rsplit(": linguist-generated:", 1)[0])
    return marked


def exclude_reason(p: str, binary: bool, generated: set[str]) -> str | None:
    base = path_parts(p)[-1]
    if binary:
        return "binary"
    if base in LOCKFILES:
        return "lockfile"
    if is_vendored(p):
        return "vendored/generated tree"
    if any(fnmatch.fnmatch(base, g) for g in GENERATED_GLOBS):
        return "generated"
    if p in generated:
        return "linguist-generated"
    return None


# ------------------------------------------------------------- risk signals

# Each entry: (signal name, path substrings, content regexes)
ESCALATING_PATH = {
    "auth/authz/crypto/session": (
        "auth", "authz", "authn", "login", "session", "token", "password",
        "passwd", "credential", "crypto", "cipher", "jwt", "oauth", "saml",
        "permission", "acl", "rbac", "secret",
    ),
    "schema migration / irreversible data op": (
        "migration", "migrate", "schema", "alembic", "flyway", "liquibase",
    ),
    "money / units / rounding": (
        "money", "currency", "price", "pricing", "invoice", "billing",
        "payment", "tax", "amount", "decimal",
    ),
    "config / environment defaults": (
        ".env", "config", "settings", "dockerfile", "docker-compose",
        "values.yaml", "helm", "terraform", ".tf", "kustomization",
    ),
}

CONCURRENCY_TOKENS = (
    "mutex", "rwlock", "lock(", "unlock", "semaphore", "atomic",
    "threading", "goroutine", "go func", "spawn(", "asyncio",
    "begin transaction", "begin()", "commit()", "rollback",
    "Interlocked", "synchronized", "volatile", "CAS", "compare_and_swap",
)

PUBLIC_API_BASES = (
    "index.ts", "index.js", "index.tsx", "mod.rs", "lib.rs", "__init__.py",
    "api.ts", "api.py", "public.ts",
)

# "deletes substantially more than it adds" — pinned so it doesn't wobble.
NET_NEGATIVE_RATIO = 2.0
NET_NEGATIVE_MIN_DELTA = 25

# no-test-changes only matters once the diff is big enough to expect tests
NO_TESTS_MIN_LINES = 50


def detect_signals(files: list[dict], diff_text: str) -> tuple[list, list, list, dict]:
    escalating: list[str] = []
    noting: list[str] = []
    judgment: list[str] = []
    per_file: dict[str, list[str]] = {}

    reviewable = [f for f in files if not f["excluded"]]

    def attribute(path: str, label: str) -> None:
        per_file.setdefault(path, [])
        if label not in per_file[path]:
            per_file[path].append(label)

    for label, needles in ESCALATING_PATH.items():
        hits = [
            f["path"] for f in reviewable
            if any(n in f["path"].lower() for n in needles)
        ]
        if hits:
            escalating.append(f"{label}: {', '.join(hits[:3])}"
                              + (f" (+{len(hits) - 3})" if len(hits) > 3 else ""))
            for h in hits:
                attribute(h, label)

    lowered = diff_text.lower()
    conc = [t for t in CONCURRENCY_TOKENS if t.lower() in lowered]
    if conc:
        escalating.append(
            "concurrency / transaction boundary: " + ", ".join(sorted(set(conc))[:4])
        )

    adds = sum(f["additions"] for f in reviewable)
    dels = sum(f["deletions"] for f in reviewable)
    if dels >= adds * NET_NEGATIVE_RATIO and (dels - adds) >= NET_NEGATIVE_MIN_DELTA:
        escalating.append(
            f"net-negative diff (+{adds}/-{dels}) — removed guards hide here"
        )

    lockfiles = [f["path"] for f in files if f["exclude_reason"] == "lockfile"]
    if lockfiles:
        noting.append(f"dependency change: {', '.join(lockfiles)}")

    total = adds + dels
    test_lines = sum(
        f["additions"] + f["deletions"] for f in reviewable if f["is_test"]
    )
    no_tests = total >= NO_TESTS_MIN_LINES and test_lines == 0
    if no_tests:
        escalating.append(
            f"no test changes across {total} reviewable lines — "
            "this review is the only gate"
        )

    api_hits = [
        f["path"] for f in reviewable
        if path_parts(f["path"])[-1] in PUBLIC_API_BASES
        or f["path"].endswith(".d.ts")
    ]
    if api_hits:
        judgment.append(
            "possible public API surface (confirm whether these exports are "
            f"actually public): {', '.join(api_hits[:4])}"
        )

    return escalating, noting, judgment, per_file


# ------------------------------------------------------------------- tiering

LINE_BOUNDS = ((150, "S"), (800, "M"))
FILE_BOUNDS = ((3, "S"), (15, "M"))
ORDER = ["S", "M", "L"]


def bucket(value: int, bounds) -> str:
    for limit, tier in bounds:
        if value <= limit:
            return tier
    return "L"


def pick_tier(lines: int, files: int, escalate: bool) -> tuple[str, dict]:
    by_lines = bucket(lines, LINE_BOUNDS)
    by_files = bucket(files, FILE_BOUNDS)
    # worse of the two dimensions
    base = ORDER[max(ORDER.index(by_lines), ORDER.index(by_files))]
    final = base
    if escalate:
        final = ORDER[min(ORDER.index(base) + 1, len(ORDER) - 1)]
    return final, {
        "by_lines": by_lines,
        "by_files": by_files,
        "base": base,
        "escalated_one_step": escalate and final != base,
        "escalation_capped": escalate and final == base,
    }


# ------------------------------------------------------------------ scope

def numstat_files(*rev_args: str) -> list[dict]:
    out = git("diff", "--numstat", *rev_args)
    rows = []
    for line in out.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a, d, path = parts[0], parts[1], parts[-1]
        binary = a == "-" or d == "-"
        rows.append({
            "path": path,
            "additions": 0 if binary else int(a),
            "deletions": 0 if binary else int(d),
            "binary": binary,
        })
    return rows


def untracked_files() -> list[dict]:
    out = git("ls-files", "--others", "--exclude-standard")
    rows = []
    for path in out.splitlines():
        if not path.strip():
            continue
        additions, binary = 0, False
        try:
            with open(path, "rb") as fh:
                blob = fh.read()
            if b"\0" in blob[:8000]:
                binary = True
            else:
                additions = blob.count(b"\n") + (
                    0 if blob.endswith(b"\n") or not blob else 1
                )
        except OSError:
            continue
        rows.append({
            "path": path, "additions": additions, "deletions": 0,
            "binary": binary,
        })
    return rows


def merge(*groups: list[dict]) -> list[dict]:
    """Union by path, summing counts (a file can be both staged and unstaged)."""
    acc: dict[str, dict] = {}
    for group in groups:
        for row in group:
            cur = acc.setdefault(
                row["path"],
                {"path": row["path"], "additions": 0, "deletions": 0,
                 "binary": False},
            )
            cur["additions"] += row["additions"]
            cur["deletions"] += row["deletions"]
            cur["binary"] = cur["binary"] or row["binary"]
    return sorted(acc.values(), key=lambda r: r["path"])


def resolve(args) -> dict:
    if not in_repo():
        return {"ok": False, "error": "not a git repository",
                "detail": "fix-code has no way to know what 'changed' means "
                          "without one. This is a hard stop."}

    if not has_commits():
        rows = untracked_files()
        return {"ok": True, "mode": "unborn-HEAD", "base": None,
                "base_how": "repository has no commits; everything is new",
                "rows": rows}

    if args.range:
        return {"ok": True, "mode": "explicit-range", "base": args.range,
                "base_how": "user-supplied", "rows": numstat_files(args.range)}

    dirty = is_dirty()
    if dirty and not args.branch:
        # HEAD..worktree covers staged+unstaged; untracked added separately
        return {"ok": True, "mode": "working-tree", "base": "HEAD",
                "base_how": "dirty tree",
                "rows": merge(numstat_files("HEAD"), untracked_files())}

    ref, how = default_branch()
    if ref is None:
        return {"ok": False, "error": "cannot determine the default branch",
                "detail": how}

    base = git("merge-base", "HEAD", ref, check=False).strip()
    if not base:
        return {"ok": False, "error": f"no merge base between HEAD and {ref}",
                "detail": "unrelated histories?"}

    rows = numstat_files(f"{base}..HEAD")
    if not rows:
        return {"ok": True, "mode": "nothing-to-review", "base": ref,
                "base_how": how, "rows": []}
    return {"ok": True, "mode": "branch", "base": f"{base} ({ref})",
            "base_how": how, "rows": rows}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--range")
    ap.add_argument("--branch", action="store_true")
    ap.add_argument("--paths", nargs="*", default=[])
    args = ap.parse_args()

    try:
        res = resolve(args)
    except GitError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2))
        return 1

    if not res["ok"]:
        print(json.dumps(res, indent=2))
        return 1

    rows = res["rows"]
    if args.paths:
        pref = tuple(p.rstrip("/") for p in args.paths)
        rows = [
            r for r in rows
            if r["path"].startswith(pref) or r["path"] in args.paths
        ]

    generated = linguist_generated([r["path"] for r in rows])
    files = []
    for r in rows:
        reason = exclude_reason(r["path"], r["binary"], generated)
        files.append({
            **r,
            "is_test": is_test(r["path"]),
            "excluded": reason is not None,
            "exclude_reason": reason,
        })

    reviewable = [f for f in files if not f["excluded"]]
    lines = sum(f["additions"] + f["deletions"] for f in reviewable)
    count = len(reviewable)

    diff_text = ""
    if res["mode"] in ("working-tree", "branch", "explicit-range"):
        rev = "HEAD" if res["mode"] == "working-tree" else (
            res["base"].split(" ")[0] + "..HEAD" if res["mode"] == "branch"
            else res["base"]
        )
        diff_text = git("diff", rev, check=False)
    escalating, noting, judgment, per_file = detect_signals(files, diff_text)

    tier, basis = pick_tier(lines, count, bool(escalating))

    print(json.dumps({
        "ok": True,
        "scope": {
            "mode": res["mode"],
            "base": res["base"],
            "base_resolved_by": res["base_how"],
        },
        "reviewable_lines": lines,
        "reviewable_files": count,
        "files": files,
        "excluded": [
            {"path": f["path"], "why": f["exclude_reason"]}
            for f in files if f["excluded"]
        ],
        "signals": {
            "escalating": escalating,
            "noting_only": noting,
            "per_file": per_file,
        },
        "judgment_candidates": judgment,
        "tier": tier,
        "tier_basis": basis,
        "announce": (
            f"{count} files, {lines} reviewable lines "
            f"({res['mode']}) · tier {tier}"
            + (f" · {len(escalating)} risk signal(s)" if escalating else "")
        ),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
