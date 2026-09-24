import os
import tempfile
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("SITE_TOKEN", "site-secret")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())
