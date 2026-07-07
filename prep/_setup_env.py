"""Create a local .env from .env.example, filling known non-secret values and
generating the two crypto secrets IN PLACE (never printed). The LinkedIn Client
Secret is left as a placeholder for the owner to paste manually."""
import secrets
from pathlib import Path

from cryptography.fernet import Fernet

root = Path(r"D:\Projects\ClaudeCode\Vision-LinkedIN")
example = (root / ".env.example").read_text(encoding="utf-8").splitlines()

# Known, non-secret values (Client ID is not a secret; it travels in the auth URL).
overrides = {
    "VISION_ENV": "dry_run",  # start in the safest mode
    "DATABASE_URL": "sqlite:///vision.db",
    "LI_CLIENT_ID": "7733f0pyvj4t41",
    "LI_CLIENT_SECRET": "PASTE_YOUR_ROTATED_SECRET_HERE",  # <-- owner edits this line
    "LI_REDIRECT_URI": "http://localhost:8000/oauth/linkedin/callback",
    "PUBLISH_MODE": "api",
    # Generated high-entropy secrets (written to file, never echoed):
    "SECRET_HMAC_KEY": secrets.token_urlsafe(48),
    "TOKEN_ENC_KEY": Fernet.generate_key().decode(),
}

out_lines = []
seen = set()
for line in example:
    if "=" in line and not line.lstrip().startswith("#"):
        key = line.split("=", 1)[0].strip()
        if key in overrides:
            out_lines.append(f"{key}={overrides[key]}")
            seen.add(key)
            continue
    out_lines.append(line)

# Ensure any override key not present in the example still gets written.
for k, v in overrides.items():
    if k not in seen:
        out_lines.append(f"{k}={v}")

(root / ".env").write_text("\n".join(out_lines) + "\n", encoding="utf-8")

# Report ONLY key names + status — never values.
print("Wrote .env. Keys set automatically:")
for k in overrides:
    if k == "LI_CLIENT_SECRET":
        print(f"  - {k:18} = <PLACEHOLDER — you paste your rotated secret>")
    elif k in ("SECRET_HMAC_KEY", "TOKEN_ENC_KEY"):
        print(f"  - {k:18} = <generated, {len(overrides[k])} chars, hidden>")
    else:
        print(f"  - {k:18} = {overrides[k]}")
print("\n.env is git-ignored (never committed).")
