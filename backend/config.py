"""
Shared configuration: paths, environment variables, constants.
"""

import os
from pathlib import Path

BASE_DIR   = Path(__file__).parent.parent
DATA_DIR   = Path(os.environ.get("RENDER_DATA_DIR", BASE_DIR))
PAPERS_DIR = DATA_DIR / "papers"
DB_PATH    = DATA_DIR / "annotations.db"
FRONTEND   = BASE_DIR / "frontend"

PAPERS_DIR.mkdir(parents=True, exist_ok=True)

ADMIN_SECRET = os.environ.get("ADMIN_SECRET", "")
ADMIN_EMAIL  = os.environ.get("ADMIN_EMAIL",  "admin@ogai.local")
ADMIN_NAME   = os.environ.get("ADMIN_NAME",   "Admin")

SESSION_COOKIE = "ogai_session"
SESSION_DAYS   = 30
PBKDF2_ITERS   = 260_000

# Rate limiting (auth endpoints)
RATE_LIMIT_WINDOW = 60   # seconds
RATE_LIMIT_MAX    = 10   # max attempts per IP per window

# SSO with TheRubricGenerator
SSO_SECRET    = os.environ.get("SSO_SECRET", "")
RUBRICGEN_URL = os.environ.get("RUBRICGEN_URL", "https://therubricgenerator.onrender.com")
