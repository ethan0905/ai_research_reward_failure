"""Sample N responses per task from the policy model and log every raw response.

  .venv/bin/python run.py --dry-run            # print a prompt + worst-case cost, no API calls
  .venv/bin/python run.py --tasks 2 --n 2      # smoke test (4 calls)
  .venv/bin/python run.py                      # full run (resumable; skips samples already logged)

Spend: before each call the worst-case cost (input estimate + max_tokens of output) is reserved
against MAX_USD; after the call it is settled with the real usage. The cumulative ledger lives in
data/spend.json and is shared with judge.py.
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import anthropic

from common import ROOT, load_config, load_env_file, load_tasks, read_jsonl

RAW_DIR = ROOT / "data" / "raw"
SPEND_PATH = ROOT / "data" / "spend.json"


class Budget:
    def __init__(self, max_usd: float):
        self.max_usd = max_usd
        self.lock = threading.Lock()
        SPEND_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.state = json.loads(SPEND_PATH.read_text()) if SPEND_PATH.exists() else {"total_usd": 0.0, "calls": 0, "by_model": {}}
        self.reserved = 0.0

    def reserve(self, usd: float) -> bool:
        with self.lock:
            if self.state["total_usd"] + self.reserved + usd > self.max_usd:
                return False
            self.reserved += usd
            return True

    def settle(self, reserved: float, actual: float, model: str):
        with self.lock:
            self.reserved -= reserved
            self.state["total_usd"] += actual
            self.state["calls"] += 1
            self.state["by_model"][model] = self.state["by_model"].get(model, 0.0) + actual
            SPEND_PATH.write_text(json.dumps(self.state, indent=2))

    @property
    def spent(self) -> float:
        return self.state["total_usd"]


def price(cfg: dict, model: str) -> tuple[float, float]:
    if model not in cfg["prices"]:
        sys.exit(f"No price for model {model!r} in config.yaml; add it before running (spend cap needs it).")
    pin, pout = cfg["prices"][model]
    return pin, pout


def cost_usd(cfg: dict, model: str, usage) -> float:
    pin, pout = price(cfg, model)
    inp = (usage.input_tokens or 0) + (getattr(usage, "cache_creation_input_tokens", 0) or 0) * 1.25 \
        + (getattr(usage, "cache_read_input_tokens", 0) or 0) * 0.1
    return (inp * pin + (usage.output_tokens or 0) * pout) / 1e6


def build_prompt(cfg: dict, task: dict) -> str:
    return cfg["user_template"].format(id=task["id"], spec=task["spec"], tests=task["visible_tests"].rstrip())


def make_client() -> anthropic.Anthropic:
    return anthropic.Anthropic(max_retries=6, timeout=600.0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--tasks", type=int, default=None, help="only the first K tasks (smoke test)")
    ap.add_argument("--n", type=int, default=None, help="override N (smoke test only)")
    args = ap.parse_args()

    load_env_file()
    cfg = load_config()
    model = cfg["model"]
    n = args.n or cfg["n_samples"]
    tasks = load_tasks()[: args.tasks] if args.tasks else load_tasks()
    pin, pout = price(cfg, model)
    worst_per_call = (3000 * pin + cfg["max_tokens"] * pout) / 1e6

    raw_path = RAW_DIR / f"samples_{model}.jsonl"
    done = {(r["task_id"], r["sample_idx"]) for r in read_jsonl(raw_path)}
    todo = [(t, i) for t in tasks for i in range(n) if (t["id"], i) not in done]

    print(f"model={model} N={n} tasks={len(tasks)} todo={len(todo)} already_logged={len(done)}")
    print(f"MAX_USD={cfg['max_usd']:.2f}  worst-case per call=${worst_per_call:.3f}  worst-case for todo=${worst_per_call * len(todo):.2f}")
    if args.dry_run:
        print("\n--- system ---\n" + cfg["system_prompt"] + "\n--- user (first task) ---\n" + build_prompt(cfg, tasks[0]))
        return

    budget = Budget(cfg["max_usd"])
    print(f"spent so far (ledger): ${budget.spent:.2f}")
    client = make_client()
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    write_lock = threading.Lock()
    stop = threading.Event()
    stats = {"ok": 0, "err": 0, "skipped_budget": 0}

    def one(task: dict, idx: int):
        if stop.is_set():
            return
        if not budget.reserve(worst_per_call):
            stop.set()
            stats["skipped_budget"] += 1
            return
        user = build_prompt(cfg, task)
        params = dict(model=model, max_tokens=cfg["max_tokens"], system=cfg["system_prompt"],
                      messages=[{"role": "user", "content": user}],
                      thinking={"type": "adaptive", "display": cfg["thinking_display"]},
                      output_config={"effort": cfg["effort"]})
        t0 = time.time()
        try:
            msg = client.messages.create(**params)
        except anthropic.BadRequestError as e:
            budget.settle(worst_per_call, 0.0, model)
            stop.set()  # a 400 means the request shape is wrong for this model: fix before burning budget
            _log_error(task, idx, model, f"BadRequest: {e.message}", write_lock)
            stats["err"] += 1
            return
        except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
            budget.settle(worst_per_call, 0.0, model)
            _log_error(task, idx, model, f"{type(e).__name__}: {e}", write_lock)
            stats["err"] += 1
            return
        c = cost_usd(cfg, model, msg.usage)
        budget.settle(worst_per_call, c, model)
        text = "".join(b.text for b in msg.content if b.type == "text")
        thinking = [b.thinking for b in msg.content if b.type == "thinking"]
        sd = getattr(msg, "stop_details", None)
        rec = {
            "task_id": task["id"], "sample_idx": idx, "model": model, "served_model": msg.model,
            "request_id": msg._request_id, "timestamp": datetime.now(timezone.utc).isoformat(),
            "latency_s": round(time.time() - t0, 2),
            "params": {k: v for k, v in params.items() if k not in ("messages", "system")},
            "system": cfg["system_prompt"], "prompt": user,
            "response_text": text, "thinking_summaries": thinking,
            "stop_reason": msg.stop_reason, "stop_details": sd.to_dict() if sd is not None and hasattr(sd, "to_dict") else sd,
            "usage": msg.usage.to_dict(), "cost_usd": round(c, 6), "sdk_version": anthropic.__version__,
        }
        with write_lock, open(raw_path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        stats["ok"] += 1

    with ThreadPoolExecutor(max_workers=cfg["concurrency"]) as ex:
        futs = [ex.submit(one, t, i) for t, i in todo]
        for k, fu in enumerate(as_completed(futs), 1):
            fu.result()
            if k % 20 == 0 or k == len(futs):
                print(f"  {k}/{len(futs)} done  ok={stats['ok']} err={stats['err']}  spent=${budget.spent:.2f}", flush=True)

    print(f"finished: ok={stats['ok']} errors={stats['err']} spent_total=${budget.spent:.2f}")
    if stop.is_set():
        print("STOPPED EARLY: budget cap reached or a 400 error (see data/raw/errors.jsonl). Re-run to resume.")


def _log_error(task, idx, model, err, lock):
    with lock, open(RAW_DIR / "errors.jsonl", "a") as f:
        f.write(json.dumps({"task_id": task["id"], "sample_idx": idx, "model": model, "error": err,
                            "timestamp": datetime.now(timezone.utc).isoformat()}) + "\n")


if __name__ == "__main__":
    main()
