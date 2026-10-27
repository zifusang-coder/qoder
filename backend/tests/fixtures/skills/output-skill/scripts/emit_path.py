import json
import sys

payload = json.load(sys.stdin)
output_dir = payload["inputs"]["output_dir"]
print(json.dumps({"path": f"{output_dir}/cover.png"}))
