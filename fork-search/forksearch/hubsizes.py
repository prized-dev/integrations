"""hubsizes.py HUB_JSON_DIR TB21_DIR OUT_JSON: compressed image size per task (Docker Hub tag API) and its digest."""
import json, sys, tomllib, pathlib
d, tb, out = map(pathlib.Path, sys.argv[1:4])
rows = {}
for f in sorted(d.glob("*.json")):
    j = json.loads(f.read_text())
    meta = tomllib.loads((tb / "tasks" / f.stem / "task.toml").read_text())
    imgs = [i for i in j.get("images", []) if i.get("architecture") == "amd64"]
    rows[f.stem] = {
        "image": meta["environment"]["docker_image"],
        "full_size_mb": round(j.get("full_size", 0) / 2**20, 1),
        "amd64_size_mb": round(imgs[0]["size"] / 2**20, 1) if imgs else None,
        "tag_digest": j.get("digest"),
        "tag_last_pushed": j.get("tag_last_pushed"),
        "cpus": meta["environment"].get("cpus"), "memory_mb": meta["environment"].get("memory_mb"),
        "storage_mb": meta["environment"].get("storage_mb"),
        "agent_timeout_s": meta.get("agent", {}).get("timeout_sec"), "verifier_timeout_s": meta.get("verifier", {}).get("timeout_sec"),
    }
out.write_text(json.dumps(rows, indent=1))
s = sorted(r["full_size_mb"] for r in rows.values())
print(len(rows), "min", s[0], "median", s[len(s)//2], "p90", s[int(.9*len(s))], "max", s[-1], "sum GB", round(sum(s)/1024, 1))
print(sorted(((r["full_size_mb"], k) for k, r in rows.items()), reverse=True)[:8])
