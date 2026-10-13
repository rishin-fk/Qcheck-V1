import duckdb
import json
from collections import defaultdict

con = duckdb.connect("tl.db", read_only=True)
paths = defaultdict(set)

def walk(node, path):
    if isinstance(node, dict):
        for k, v in node.items():
            walk(v, f"{path}.{k}" if path else k)
    elif isinstance(node, list):
        for item in node:
            walk(item, f"{path}[]")
    else:
        paths[path].add("null" if node is None else type(node).__name__)

rows = con.execute("SELECT raw_json FROM tl_detail").fetchall()
for (raw,) in rows:
    walk(json.loads(raw), "")

print(f"{len(rows)} rows\n")
print(f"{'path':60} types")
for p in sorted(paths):
    print(f"{p:60} {', '.join(sorted(paths[p]))}")

print("\nSample row (pretty):")
sample = json.loads(rows[0][0])
print(json.dumps(sample, indent=2, ensure_ascii=False)[:4000])

con.close()