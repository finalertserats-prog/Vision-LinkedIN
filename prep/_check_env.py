"""Validate .env is ready for the LinkedIn spike WITHOUT printing any secret."""
from pathlib import Path

env = {}
for line in (Path(r"D:\Projects\ClaudeCode\Vision-LinkedIN") / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.lstrip().startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()


def mask(v):
    if not v:
        return "<EMPTY>"
    return f"set, {len(v)} chars, starts '{v[:4]}...'"


ok = True


def check(name, cond, detail):
    global ok
    mark = "OK " if cond else "!! "
    if not cond:
        ok = False
    print(f"  [{mark}] {name}: {detail}")


sec = env.get("LI_CLIENT_SECRET", "")
check("LI_CLIENT_SECRET", sec and sec != "PASTE_YOUR_ROTATED_SECRET_HERE" and len(sec) > 10,
      mask(sec) if sec != "PASTE_YOUR_ROTATED_SECRET_HERE" else "<STILL PLACEHOLDER — not pasted>")
check("  -> looks like a LinkedIn secret", sec.startswith("WPL"),
      "starts with 'WPL'" if sec.startswith("WPL") else "does NOT start with 'WPL' (double-check you copied the right value)")
check("LI_CLIENT_ID", bool(env.get("LI_CLIENT_ID")) and env["LI_CLIENT_ID"] != "your-linkedin-client-id", env.get("LI_CLIENT_ID", ""))
check("LI_REDIRECT_URI", env.get("LI_REDIRECT_URI") == "http://localhost:8000/oauth/linkedin/callback", env.get("LI_REDIRECT_URI", ""))
check("LI_VERSION", bool(env.get("LI_VERSION")), env.get("LI_VERSION", ""))
check("TOKEN_ENC_KEY", len(env.get("TOKEN_ENC_KEY", "")) >= 30, mask(env.get("TOKEN_ENC_KEY", "")))
check("SECRET_HMAC_KEY", len(env.get("SECRET_HMAC_KEY", "")) >= 30, mask(env.get("SECRET_HMAC_KEY", "")))
check("VISION_ENV", env.get("VISION_ENV") in ("dry_run", "staging", "live"), env.get("VISION_ENV", ""))

print("\nRESULT:", "READY for the LinkedIn spike ✅" if ok else "NOT ready — fix the !! lines above")
