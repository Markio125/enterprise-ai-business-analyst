"""Generate the synthetic payments warehouse (DuckDB)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from analyst.datagen import DEFAULT_CONFIG, DEFAULT_OUT, generate  # noqa: E402


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--out", default=str(DEFAULT_OUT))
    p.add_argument("--scale", type=float, default=1.0, help="multiply daily volume (e.g. 0.2 for a quick build)")
    args = p.parse_args()
    manifest = generate(args.config, args.out, args.scale)
    manifest_path = Path(args.out).with_name("generation_manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
