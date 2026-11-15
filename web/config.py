"""Settings for the website, from environment variables or web/admin.env.

On this PC everything lives in web/admin.env (created on first start). On Vercel the same names are set as
environment variables in the project settings; a real environment variable always wins over the file.

    ADMIN_PASSWORD, SECRET_KEY, UPLOAD_TOKEN      admin login, session cookies, upload.py's token
    DATABASE_URL                                  postgresql://... (Neon); empty = SQLite in web/data
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID,              Cloudflare R2 bucket for clips, thumbnails, avatars;
    R2_SECRET_ACCESS_KEY, R2_BUCKET, MEDIA_URL    empty = files on this PC in web/data/media
"""
import os
import secrets

HERE = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(HERE, "admin.env")
SECRET_NAMES = ("ADMIN_PASSWORD", "SECRET_KEY", "UPLOAD_TOKEN")
ON_VERCEL = bool(os.environ.get("VERCEL"))


def read_env_file(path=ENV_FILE):
    vals = {}
    if os.path.isfile(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
    return vals


def load():
    """Put admin.env's values into os.environ (without overriding real environment variables)."""
    for k, v in read_env_file().items():
        if v and not os.environ.get(k):
            os.environ[k] = v


def ensure_secrets():
    """Admin password, session key and upload token. On this PC, missing ones are created in admin.env."""
    missing = [k for k in SECRET_NAMES if not os.environ.get(k)]
    if missing and ON_VERCEL:
        raise RuntimeError(f"Set {', '.join(missing)} in the Vercel project's environment variables.")
    if missing:
        vals = read_env_file()
        new = {"ADMIN_PASSWORD": secrets.token_urlsafe(12), "SECRET_KEY": secrets.token_hex(32),
               "UPLOAD_TOKEN": secrets.token_urlsafe(32)}
        for k in missing:
            os.environ[k] = vals[k] = new[k]
        lines = open(ENV_FILE, encoding="utf-8").read().splitlines() if os.path.isfile(ENV_FILE) else [
            "# Secrets for the website. Change ADMIN_PASSWORD if you like, then restart.",
            "# UPLOAD_TOKEN lets upload.py on the rendering PC send matches to the site."]
        lines = [ln for ln in lines if ln.split("=", 1)[0].strip() not in missing]
        lines += [f"{k}={vals[k]}" for k in missing]
        with open(ENV_FILE, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    return {k: os.environ[k] for k in SECRET_NAMES}


load()
