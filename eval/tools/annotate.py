"""Print a results JSON file as one GitHub Actions notice annotation.

Annotations can be read from the public check-runs API without repository access, so a
run's numbers can be collected and committed by anyone who can see the run.

    python eval/tools/annotate.py results-final.json
"""

from __future__ import annotations

import json
import sys

for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as fh:
        body = json.dumps(json.load(fh), separators=(",", ":"))
    body = body.replace("%", "%25").replace("\r", "").replace("\n", "%0A")
    print(f"::notice title={path}::{body}")
