import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("ADMIN_PASSWORD", "test-pass")
os.environ.setdefault("SECRET_KEY", "test-secret")
os.environ.setdefault("AGENT_TOKEN", "agent-token")
os.environ.setdefault("API_TOKEN", "api-token")
