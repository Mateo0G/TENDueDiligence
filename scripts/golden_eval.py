"""Score a job against a golden-facts set and print its cost report.

    python scripts/golden_eval.py <job_id> [golden_name] [--base URL] [--out DIR]

Runs the scoring on the server (POST /jobs/{id}/golden-eval -- the grading
call needs the deployed API key), saves golden_<job>.json and
cost_report_<job>.json to --out (default: current directory), and prints a
summary: pass rate, failed facts, false "missing" claims, forbidden text,
page coverage, and cost by stage.
"""
import argparse
import json
import os
import urllib.error
import urllib.request

p = argparse.ArgumentParser()
p.add_argument("job_id")
p.add_argument("name", nargs="?", default="cari_health")
p.add_argument("--base", default="https://tenduediligence-production.up.railway.app")
p.add_argument("--out", default=".")
a = p.parse_args()


def call(method, path):
    req = urllib.request.Request(a.base + path, method=method)
    try:
        with urllib.request.urlopen(req, timeout=600) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise SystemExit(f"{method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:1000]}")


g = call("POST", f"/jobs/{a.job_id}/golden-eval?name={a.name}")
c = call("GET", f"/jobs/{a.job_id}/cost_report.json")
os.makedirs(a.out, exist_ok=True)
json.dump(g, open(os.path.join(a.out, f"golden_{a.job_id}.json"), "w"), indent=2)
json.dump(c, open(os.path.join(a.out, f"cost_report_{a.job_id}.json"), "w"), indent=2)

print(f"GOLDEN {g['golden']}: {g['passed']}/{g['facts']} = {g['pass_rate']:.0%}")
for r in g["results"]:
    if not r["pass"]:
        where = " (found in another report)" if r.get("found_elsewhere") else ""
        print(f"  FAIL {r['id']:24} [{', '.join(r['report'])}]{where}")
print(f"False 'missing' claims: {len(g['false_missing_claims'])}")
for m in g["false_missing_claims"][:15]:
    print(f"  [{m['report']}] {m['document']}: {m['sentence'][:160]}")
print(f"Contradictions: {len(g.get('contradictions', []))}")
for m in g.get("contradictions", [])[:15]:
    print(f"  [{m['report']}] {m['id']}: {m['sentence'][:160]}")
print(f"Forbidden text: {g['forbidden'] or 'none'}")
cov = g["coverage"]
print(f"Coverage: {cov['files']} files, {len(cov['short'])} short " + str(cov["short"][:8]))
print(f"\nCOST report total ${c['report_total_usd']:.2f} (evaluation ${c['evaluation_total_usd']:.2f})")
for stage, v in c["by_stage"].items():
    print(f"  ${v['cost_usd']:6.2f}  {stage:30} calls={v['calls']} in={v['input_tokens']:,} out={v['output_tokens']:,} "
          f"cache_w={v['cache_write_tokens']:,} cache_r={v['cache_read_tokens']:,} web={v['web_searches']}")
