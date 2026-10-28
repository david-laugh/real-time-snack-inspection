"""Download a SAM2.1 checkpoint into services/c3-inference/models/ (git-ignored).

    python scripts/download_checkpoint.py            # tiny (default)
    python scripts/download_checkpoint.py small
"""

import sys
import urllib.request
from pathlib import Path

BASE = "https://dl.fbaipublicfiles.com/segment_anything_2/092824"
VARIANTS = {
    "tiny": ("sam2.1_hiera_tiny.pt", "configs/sam2.1/sam2.1_hiera_t.yaml"),
    "small": ("sam2.1_hiera_small.pt", "configs/sam2.1/sam2.1_hiera_s.yaml"),
    "base_plus": ("sam2.1_hiera_base_plus.pt", "configs/sam2.1/sam2.1_hiera_b+.yaml"),
    "large": ("sam2.1_hiera_large.pt", "configs/sam2.1/sam2.1_hiera_l.yaml"),
}

if __name__ == "__main__":
    variant = sys.argv[1] if len(sys.argv) > 1 else "tiny"
    if variant not in VARIANTS:
        sys.exit(f"unknown variant {variant!r}; choose from {', '.join(VARIANTS)}")
    name, cfg = VARIANTS[variant]
    dest = Path(__file__).resolve().parents[1] / "models" / name
    if dest.exists():
        print(f"already present: {dest}")
    else:
        print(f"downloading {BASE}/{name} -> {dest}")
        urllib.request.urlretrieve(f"{BASE}/{name}", dest)
    print(f"\nset in .env:\n  SAM2_MODEL_CFG={cfg}\n  SAM2_CHECKPOINT_PATH=/app/models/{name}")
