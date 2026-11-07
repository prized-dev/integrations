"""shapes.py HUBSIZES_JSON OUT_JSON: per-task sandbox shape for classification.

Disk = the task's storage (10 GB) + headroom, the headroom sized to the image:
4 x its compressed size on Docker Hub (unpacked images run 2-3.5 x) + 3 GB
(the workdir tar and slack). torch-tensor-parallelism needs 4 vCPUs (gloo's
teardown deadlocks on fewer). Stdlib only.
"""
import json
import math
import sys

MIN_VCPU = {"torch-tensor-parallelism": 4}

src, out = sys.argv[1], sys.argv[2]
rows = json.load(open(src))
table = {}
for task, r in sorted(rows.items()):
    gb = (r.get("amd64_size_mb") or r["full_size_mb"]) / 1024
    shape = {"image_headroom_gb": math.ceil(4 * gb) + 3, "compressed_mb": r.get("amd64_size_mb") or r["full_size_mb"],
             "digest": r["tag_digest"]}
    if task in MIN_VCPU:
        shape["min_vcpu"] = MIN_VCPU[task]
    table[task] = shape
json.dump(table, open(out, "w"), indent=1, sort_keys=True)
h = sorted(v["image_headroom_gb"] for v in table.values())
print(len(table), "headroom GB min", h[0], "median", h[len(h) // 2], "max", h[-1], "sum disk (10+h) GB", sum(10 + x for x in h))
