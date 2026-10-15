import os
import sys
from pathlib import Path

os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789")
os.environ.setdefault("ADMIN_USERNAME", "admin")
os.environ.setdefault("ADMIN_PASSWORD", "test-password")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
