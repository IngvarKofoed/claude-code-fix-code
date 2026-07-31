#!/usr/bin/env python3
"""Pre-flight plan for a /fix-code L-tier run.

Prints the worker table the user sees *before* any agent is spawned, and emits
the same slices back with a resolved `model` and `risk` on each one. The workflow
script honors those fields, which is the point: if the table were computed
separately from what the workflow does, it would become a lie the first time the
two drifted. One source of truth, printed and passed.

Usage:
  plan.py --triage triage.json --slices slices.json
  plan.py --triage triage.json --slices slices.json --json   # machine-readable only

slices.json is the reviewer's judgment call and looks like:
  [ {"lens": "correctness", "label": "worldgen", "files": ["src/world/gen.ts"]},
    {"lens": "integration", "label": "contracts", "files": ["src/api/dto.ts"]} ]
"""

from __future__ import annotations

import argparse
import json
import sys

# Kept in sync with review-L.workflow.js, which falls back to this same rule when
# a slice arrives without a resolved model.
FINDER_RISK = "opus"
FINDER_PLAIN = "sonnet"
VERIFIER = "sonnet"
SYNTH = "opus"

PRETTY = {"opus": "Opus 5", "sonnet": "Sonnet 5", "haiku": "Haiku 4.5"}

CAP = 6

# The only token constant left, and the only one that does any work: it is
# compared against `budget.remaining()`, which counts OUTPUT tokens exclusively.
# An estimate, not a measurement — it only matters when a per-turn budget is set.
#
# There is deliberately no estimate of a worker's *total* token consumption. Such
# a number would be guesswork, and worse, summing it across a fan-out that mixes
# Opus and Sonnet workers yields a figure that corresponds to no real cost — the
# tiers are priced differently, so tokens don't add up into anything spendable.
# The model column in the table is the honest cost signal.
PER_WORKER_OUTPUT = 12_000


def resolve(slices: list[dict], per_file: dict[str, list[str]]) -> list[dict]:
    out = []
    for s in slices:
        hits: list[str] = []
        for f in s.get("files", []):
            for label in per_file.get(f, []):
                if label not in hits:
                    hits.append(label)
        out.append({
            **s,
            "risk": bool(hits),
            "risk_reasons": hits,
            "model": FINDER_RISK if hits else FINDER_PLAIN,
        })
    return out


def table(resolved: list[dict], dropped: list[dict]) -> str:
    rows = ["| Worker | Phase | Risk | Model |", "| --- | --- | --- | --- |"]
    for s in resolved:
        risk = ", ".join(s["risk_reasons"]) if s["risk"] else "—"
        rows.append(
            f"| find:{s['lens']}/{s['label']} | Find | {risk} | {PRETTY[s['model']]} |"
        )
    for s in dropped:
        rows.append(
            f"| ~~find:{s['lens']}/{s['label']}~~ | Find | dropped — budget | — |"
        )
    rows.append(
        f"| verify (one per finding) | Verify | — | {PRETTY[VERIFIER]} |"
    )
    rows.append(f"| gap-sweep | Gap sweep | — | {PRETTY[SYNTH]} |")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--triage", required=True)
    ap.add_argument("--slices", required=True)
    ap.add_argument("--budget", type=int, default=0,
                    help="remaining OUTPUT-token budget (budget.remaining()), if set")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    triage = json.load(open(args.triage))
    slices = json.load(open(args.slices))
    per_file = triage.get("signals", {}).get("per_file", {})

    resolved = resolve(slices, per_file)

    # Risk-flagged slices go first so that if the budget truncates the run, the
    # slices that most needed reading are the ones that got read.
    resolved.sort(key=lambda s: (not s["risk"], s["lens"], s["label"]))

    limit = CAP
    if args.budget:
        limit = min(CAP, max(1, int(args.budget * 0.7 // PER_WORKER_OUTPUT)))
    run, dropped = resolved[:limit], resolved[limit:]

    if args.json:
        print(json.dumps({"slices": run, "dropped": dropped}, indent=2))
        return 0

    tier = triage.get("tier", "?")
    print(f"Planned run — tier {tier}, {len(run)} find-workers"
          + (f", {len(dropped)} dropped for budget" if dropped else ""))
    print()
    print(table(run, dropped))
    print()
    mix: dict[str, int] = {}
    for s in run:
        mix[s["model"]] = mix.get(s["model"], 0) + 1
    print("Find-workers: "
          + ", ".join(f"{n} x {PRETTY[m]}" for m, n in sorted(mix.items()))
          + ". Verify spawns one Sonnet 5 worker per finding, so it scales with "
            "findings rather than diff size.")
    if dropped:
        print(f"NOT reviewed: "
              + ", ".join(f"{s['lens']}/{s['label']}" for s in dropped)
              + " — say so in the report header.")
    print()
    print("Pass these slices to the workflow verbatim; they carry the resolved "
          "model so the table above matches what actually runs:")
    print(json.dumps({"slices": run}, indent=2))
    # The table is the one artifact of this script the *user* is supposed to read,
    # and it is printed to a place the user can't read — Bash stdout goes to the
    # agent. The observed failure is the agent treating "the script printed it" as
    # "the user saw it" and moving straight to the Workflow call, which happens
    # specifically when a CLAUDE.md rule invoked the review mid-flow rather than
    # the user asking for it. So the last thing on stdout, where attention lands
    # just before the agent writes its reply, is the instruction to relay it.
    print()
    print("[FOR THE REVIEWING AGENT — do not paste this notice into your reply]")
    print("The user cannot see this output. Re-emit the header line and the table "
          "above, verbatim and inside a fenced code block, in your own reply "
          "BEFORE spawning anything. \"The plan is above\" does not show it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
