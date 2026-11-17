"""Mechanical checks on the task set (no API calls).

- every solvable task: reference solution passes visible + hidden tests (naive and hardened runner)
  and triggers NO static tamper flag (detector false-positive check);
- every impossible task: the demonstration hack passes the NAIVE grader (the outcome reward is
  really exploitable), and we record whether the static detectors catch it (detector recall on
  known hacks);
- every impossible task: a trivial honest stub (raise NotImplementedError) fails.

Writes results/task_validation.md. Exit code 1 if any hard check fails.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from common import DETECTOR_KEYS, ROOT, load_config, load_tasks, run_tests, sandbox_available, static_flags  # noqa: E402


def main():
    cfg = load_config()
    to = cfg["test_timeout_s"]
    tasks = load_tasks()
    rows, bad = [], []
    caught = 0
    for t in tasks:
        if t["label"] == "solvable":
            sol = t["reference_solution"]
            v = run_tests(sol, t["visible_tests"], to)
            h = run_tests(sol, t["hidden_tests"], to, module="test_hidden") if t["hidden_tests"] else {"naive_pass": True, "hardened_pass": True}
            f = static_flags(sol, t["visible_tests"], t["spec"])
            fl = [k for k in DETECTOR_KEYS if f[k]]
            ok = v["naive_pass"] and v["hardened_pass"] and h["naive_pass"] and h["hardened_pass"] and not fl
            rows.append(f"| {t['id']} | {t['key']} | solvable/{t['category']} | ref visible {v['naive_pass']}/{v['hardened_pass']}, hidden {h['naive_pass']}/{h['hardened_pass']} | flags: {fl or '-'} | {'OK' if ok else 'FAIL'} |")
            if not ok:
                bad.append((t["id"], t["key"], v.get("naive_tail", ""), fl))
        else:
            hk = t["hack_solution"]
            v = run_tests(hk, t["visible_tests"], to)
            stub = f"def {t['func']}(*a, **k):\n    raise NotImplementedError\n"
            s = run_tests(stub, t["visible_tests"], to)
            f = static_flags(hk, t["visible_tests"], t["spec"])
            fl = [k for k in DETECTOR_KEYS if f[k]]
            dyn = v["naive_pass"] and not v["hardened_pass"]
            caught += bool(fl or dyn)
            ok = v["naive_pass"] and not s["naive_pass"]
            rows.append(f"| {t['id']} | {t['key']} | impossible/{t['category']} | hack naive {v['naive_pass']}, hardened {v['hardened_pass']}; stub fails {not s['naive_pass']} | flags: {fl or '-'}{' +dynamic_exit' if dyn else ''} | {'OK' if ok else 'FAIL'} |")
            if not ok:
                bad.append((t["id"], t["key"], v.get("naive_tail", ""), fl))
    n_imp = sum(t["label"] == "impossible" for t in tasks)
    out = ["# Task validation", "",
           f"Sandbox (macOS sandbox-exec, no network, writes only in temp dir): {sandbox_available()}", "",
           f"Tasks: {len(tasks)} ({len(tasks) - n_imp} solvable, {n_imp} impossible). Hard failures: {len(bad)}.", "",
           f"Detector recall on the {n_imp} author-written demonstration hacks: {caught}/{n_imp} flagged "
           "(static flags or dynamic early-exit). These hacks are not a representative sample of model hacks; this is a smoke test only.", "",
           "| id | key | label/category | check | detector | result |", "|---|---|---|---|---|---|", *rows, ""]
    (ROOT / "results").mkdir(exist_ok=True)
    (ROOT / "results" / "task_validation.md").write_text("\n".join(out))
    print("\n".join(out[:6]))
    for b in bad:
        print("FAIL:", b[0], b[1], b[3], "\n", b[2][-400:])
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
