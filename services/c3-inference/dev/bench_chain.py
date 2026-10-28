"""Real-chain load test for C3 scale-out (stdlib only; see dev/README.md).

    C1 loadgen (WS, 30 fps x clients) -> MQTT -> C2 -> Nginx -> C3 x N (GPU) -> C4 (host :8003)

For each REPLICAS:C2_MAX_INFLIGHT:CLIENTS item it (re)starts the containers, sends 3 s of
warm-up traffic, then measures DURATION seconds and collects per-run deltas from
C1 /api/metrics, C2 /stats, C3 /metrics + structured logs, the Nginx access log,
C4 /api/stats and nvidia-smi. C1/C2/C4 code is only run, never modified.

    python services/c3-inference/dev/bench_chain.py --plan 1:2:1,2:3:1,3:4:2 --duration 30 --out results.json
"""
import argparse
import json
import os
import statistics
import subprocess
import threading
import time
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--plan", default="1:2:1,2:3:1,3:4:1",
                help="comma list of REPLICAS:C2_MAX_INFLIGHT:CLIENTS (each client streams 30 fps)")
ap.add_argument("--duration", type=float, default=30, help="measured seconds per item")
ap.add_argument("--out", default="bench_chain_results.json")
ap.add_argument("--mqtt-host-port", default="11884", help="host port for the compose MQTT broker")
opts = ap.parse_args()

REPO = str(Path(__file__).resolve().parents[3])
DURATION = opts.duration
FILES = ["-f", "docker-compose.yml", "-f", "services/c3-inference/compose.gpu.yml",
         "-f", "services/c3-inference/dev/chain.override.yml"]


def dc(*args, env_extra=None, check=True, capture=True):
    env = dict(os.environ, MQTT_HOST_PORT=opts.mqtt_host_port, **(env_extra or {}))
    r = subprocess.run(["docker", "compose", *FILES, *args], cwd=REPO, env=env, capture_output=capture, text=True,
                       encoding="utf-8", errors="replace")
    if check and r.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{r.stderr[-2000:]}")
    return r.stdout


def http_json(url, method="GET"):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def in_container(service, code):
    return json.loads(dc("exec", "-T", service, "python", "-c", code))


def c4_detections():
    """C4 (C4-Visualizer branch) counts detections in /api/stats total. None if C4 is not running."""
    try:
        return http_json("http://127.0.0.1:8003/api/stats")["total"]
    except OSError:
        return None


def c3_metrics(i):
    return in_container(f"c3-inference-{i}", "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8002/metrics').read().decode())")


def wait_healthy(services, timeout=240):
    t0 = time.time()
    while time.time() - t0 < timeout:
        ps = dc("ps", "--format", "json")
        rows = [json.loads(l) for l in ps.splitlines() if l.strip()]
        st = {r["Service"]: r.get("Health", "") for r in rows}
        if all(st.get(s) == "healthy" for s in services):
            return time.time() - t0
        time.sleep(3)
    raise RuntimeError(f"not healthy: {st}")


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    return round(s[min(len(s) - 1, int(round(q / 100 * (len(s) - 1))))], 2)


def gpu_sampler(stop, samples):
    while not stop.is_set():
        out = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True).stdout.strip()
        try:
            u, m = [float(x) for x in out.split(",")]
            samples.append((u, m))
        except ValueError:
            pass
        time.sleep(0.5)


def run(replicas, inflight, clients):
    c3s = [f"c3-inference-{i}" for i in range(1, replicas + 1)]
    extra = [f"c3-inference-{i}" for i in range(replicas + 1, 4)]
    if extra:
        dc("stop", *extra)
    dc("up", "-d", "--no-deps", *c3s)
    dc("up", "-d", "--no-deps", "nginx-inference-lb", "mqtt-broker")
    dc("up", "-d", "--no-deps", "--force-recreate", "c2-preprocessing", env_extra={"C2_MAX_INFLIGHT": str(inflight)})
    dc("up", "-d", "--no-deps", "c1-gateway")
    waited = wait_healthy(c3s + ["c1-gateway"])
    time.sleep(12)  # nginx re-resolves replica DNS every 10s; C2 MQTT connect
    for _ in range(30):
        c2 = http_json("http://127.0.0.1:8001/stats")
        if c2["mqtt"].get("connected"):
            break
        time.sleep(1)
    # warm-up traffic (not measured): 3 s
    dc("exec", "-T", "c1-gateway", "python", "src/c1_gateway.py", "loadgen", "--url", "ws://localhost:8000/ws/stream",
       "--source", "/frames", "--fps", "30", "--duration", "3", "--width", "960")
    time.sleep(3)

    http_json("http://127.0.0.1:8000/api/metrics/reset", "POST")
    c2_before = Counter(http_json("http://127.0.0.1:8001/stats")["events"])
    c3_before = {i: c3_metrics(i) for i in range(1, replicas + 1)}
    c4_before = c4_detections()
    since = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    stop, gpu = threading.Event(), []
    th = threading.Thread(target=gpu_sampler, args=(stop, gpu)); th.start()
    t0 = time.time()
    loadgen = dc("exec", "-T", "c1-gateway", "python", "src/c1_gateway.py", "loadgen", "--url", "ws://localhost:8000/ws/stream",
                 "--source", "/frames", "--fps", "30", "--clients", str(clients), "--duration", str(DURATION), "--width", "960")
    elapsed = time.time() - t0
    time.sleep(4)  # drain in-flight requests + C4 pushes
    stop.set(); th.join()

    c1 = http_json("http://127.0.0.1:8000/api/metrics")
    c2_after = Counter(http_json("http://127.0.0.1:8001/stats")["events"])
    c2 = {k: c2_after[k] - c2_before[k] for k in c2_after}
    c3 = {}
    for i in range(1, replicas + 1):
        a, b = c3_metrics(i), c3_before[i]
        c3[f"c3-inference-{i}"] = {k: a[k] - b[k] for k in ("processed_frame_count", "error_count", "results_push_ok_count", "results_push_error_count")}
        c3[f"c3-inference-{i}"]["gpu_memory"] = a["gpu_memory"]
    c4_after = c4_detections()
    c4_total = c4_after - c4_before if None not in (c4_before, c4_after) else None

    # per-frame C3 timings from structured logs (this run only)
    stages, breakdown = defaultdict(list), defaultdict(list)
    for line in dc("logs", "--no-log-prefix", "--since", since, *c3s).splitlines():
        if '"event": "infer"' not in line:
            continue
        rec = json.loads(line[line.index("{"):])
        for k, v in rec["timings_ms"].items():
            stages[k].append(v)
        for k, v in (rec.get("latency_breakdown_ms") or {}).items():
            if v is not None:
                breakdown[k].append(v)
    # nginx access log (this run only)
    ng_time, ng_up, ng_status = [], Counter(), Counter()
    for line in dc("logs", "--no-log-prefix", "--since", since, "nginx-inference-lb").splitlines():
        if "POST /infer" not in line:
            continue
        parts = dict(p.split("=", 1) for p in line.split() if "=" in p)
        ng_status[line.split('"')[2].split()[0]] += 1
        ng_up[parts.get("upstream", "?")] += 1
        try:
            ng_time.append(float(parts["request_time"]) * 1000)
        except (KeyError, ValueError):
            pass

    return {
        "replicas": replicas, "c2_max_inflight": inflight, "clients": clients, "input_fps": 30 * clients, "duration_s": round(elapsed, 1), "healthy_after_s": round(waited, 1),
        "loadgen_tail": loadgen.strip().splitlines()[-3:],
        "c1": c1,
        "loadgen_out": loadgen.strip().splitlines()[-40:],
        "c2_events": c2,
        "c2_forward_fps": round(c2.get("forwarded", 0) / DURATION, 2),
        "c3": c3,
        "c3_stage_ms": {k: {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95), "max": round(max(v), 2)} for k, v in stages.items()},
        "latency_breakdown_ms": {k: {"n": len(v), "p50": pct(v, 50), "p95": pct(v, 95)} for k, v in breakdown.items()},
        "nginx": {"status": dict(ng_status), "upstream_split": dict(ng_up), "request_ms_p50": pct(ng_time, 50),
                  "request_ms_p95": pct(ng_time, 95), "request_ms_max": round(max(ng_time), 1) if ng_time else None},
        "c4_received_detections": c4_total,
        "gpu": {"util_avg": round(statistics.mean(u for u, _ in gpu), 1) if gpu else None,
                "util_max": max((u for u, _ in gpu), default=None), "mem_used_max_mib": max((m for _, m in gpu), default=None)},
    }


results = []
for item in opts.plan.split(","):
    r, n, c = (int(x) for x in item.split(":"))
    print(f"=== replicas={r} C2_MAX_INFLIGHT={n} clients={c} ({30 * c} fps)", flush=True)
    res = run(r, n, c)
    results.append(res)
    s3 = res["c3_stage_ms"]
    print(f"  C2 forwarded {res['c2_forward_fps']} fps, dropped {res['c2_events'].get('dropped_stale', 0)}, "
          f"timeout {res['c2_events'].get('timeout', 0)} | nginx p50/p95 {res['nginx']['request_ms_p50']}/"
          f"{res['nginx']['request_ms_p95']} ms | C3 queue p50 {s3.get('lock_wait_ms', {}).get('p50')} ms, "
          f"encoder p50 {s3.get('encoder_ms', {}).get('p50')} ms | GPU avg {res['gpu']['util_avg']}%", flush=True)
    json.dump(results, open(opts.out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print(f"saved {opts.out}")
