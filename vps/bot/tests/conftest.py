import os
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("LINK_TOKEN", "link-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
