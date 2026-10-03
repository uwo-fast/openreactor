import os

basedir = os.path.abspath(os.path.dirname(__file__))


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY") or "your-secret-key"
    # Kept from the project's former name so existing installs keep their data
    DATABASE_PATH = os.path.join(basedir, "loafware.db")
