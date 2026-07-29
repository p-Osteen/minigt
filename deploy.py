"""
GitHub Pages Auto-Deployment Script for MINI GT Catalog.

Reads GITHUB_TOKEN from .env file.
Commits updated catalog data and pushes to GitHub Pages.
"""
import os
import sys
import subprocess
import time
import json
from datetime import datetime


def _load_env() -> None:
    """Loads variables from .env into os.environ (no extra packages needed)."""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if not os.path.exists(env_path):
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, val = line.partition("=")
                os.environ.setdefault(key.strip(), val.strip())


def _run(args: list, cwd: str = None) -> subprocess.CompletedProcess:
    """Run a shell command and return the result."""
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def deploy(log=print) -> bool:
    """
    Full deployment pipeline:
      1. Load token from .env
      2. Configure remote with token
      3. Check for changes
      4. Stage relevant files
      5. Commit with product count in message
      6. Push with retry + exponential back-off
      7. Confirm GitHub Pages URL
    Returns True on success, False on failure.
    """
    _load_env()

    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token or token == "YOUR_GITHUB_TOKEN_HERE":
        log(
            "\n[ERROR] GITHUB_TOKEN not set.\n"
            "  Open .env and replace YOUR_GITHUB_TOKEN_HERE with your PAT.\n"
            "  Get one at: https://github.com/settings/tokens  (needs 'repo' scope)"
        )
        return False

    cwd = os.path.dirname(os.path.abspath(__file__))
    repo_url = f"https://{token}@github.com/p-Osteen/minigt.git"
    branch = "main"

    log("\n=== GitHub Pages Deployment Pipeline ===")

    # -- Git check --
    r = _run(["git", "--version"], cwd=cwd)
    if r.returncode != 0:
        log("[ERROR] git is not installed or not in PATH.")
        return False
    log(f"[OK] {r.stdout.strip()}")

    # -- Configure remote (token auth) --
    log("\n[1/6] Configuring remote URL...")
    check_origin = _run(["git", "remote", "get-url", "origin"], cwd=cwd)
    if check_origin.returncode == 0:
        r = _run(["git", "remote", "set-url", "origin", repo_url], cwd=cwd)
    else:
        r = _run(["git", "remote", "add", "origin", repo_url], cwd=cwd)
    if r.returncode != 0:
        log(f"[ERROR] Failed to configure remote: {r.stderr.strip()}")
        return False
    log("[OK] Remote configured.")

    # -- Check for changes --
    log("\n[2/6] Checking for changes...")
    r = _run(["git", "status", "--porcelain"], cwd=cwd)
    if r.returncode != 0:
        log(f"[ERROR] Git status check failed: {r.stderr.strip()}")
        return False
    if not r.stdout.strip():
        # Check if we have any commits at all (if new repo, we must commit and push)
        check_commit = _run(["git", "rev-parse", "HEAD"], cwd=cwd)
        if check_commit.returncode == 0:
            log("[INFO] No changes detected — catalog is already up to date.")
            log("\n[INFO] GitHub Pages: https://p-osteen.github.io/minigt/")
            return True
    changed = r.stdout.strip().splitlines()
    log(f"[INFO] {len(changed)} file(s) changed/untracked.")

    # -- Stage files --
    log("\n[3/6] Staging files...")
    files_to_add = [
        "database/products.json",
        "database/db_manager.py",
        "database/models.py",
        "crawler/",
        "index.html",
        "catalog_print.html",
        "static/",
        "README.md",
        "deploy.py",
        ".env.example",
    ]
    for f in files_to_add:
        _run(["git", "add", "--", f], cwd=cwd)
    log("[OK] Files staged.")

    # -- Count products for commit message --
    products_count = 0
    json_path = os.path.join(cwd, "database", "products.json")
    if os.path.exists(json_path):
        try:
            with open(json_path, encoding="utf-8") as f:
                products_count = len(json.load(f))
        except Exception:
            pass

    # -- Commit --
    log("\n[4/6] Committing...")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = f"chore: catalog update {ts} [{products_count} models]"
    r = _run(["git", "commit", "-m", msg], cwd=cwd)
    if r.returncode != 0:
        out = (r.stdout + r.stderr).strip()
        if "nothing to commit" in out.lower():
            log("[INFO] Nothing to commit.")
        else:
            log(f"[ERROR] Commit failed:\n  {out}")
            return False
    else:
        log(f"[OK] Committed: {msg}")

    # -- Push with retry --
    log(f"\n[5/6] Pushing to GitHub ({branch})...")
    for attempt in range(3):
        r = _run(["git", "push", "origin", branch], cwd=cwd)
        if r.returncode == 0:
            log("[OK] Push successful!")
            break
        err = (r.stdout + r.stderr).strip()
        log(f"[WARN] Attempt {attempt + 1}/3 failed: {err}")
        if attempt < 2:
            wait = 2 ** attempt
            log(f"[INFO] Retrying in {wait}s...")
            time.sleep(wait)
    else:
        log("[ERROR] Push failed after 3 attempts. Check your token and network.")
        return False

    # -- Done --
    log("\n[6/6] Deployment complete!")
    log(f"  Products committed: {products_count}")
    log(f"  GitHub Pages URL:   https://p-osteen.github.io/minigt/")
    log(
        "\n  NOTE: GitHub Pages may take 1-2 minutes to rebuild after first push.\n"
        "  Make sure Pages is enabled: repo Settings -> Pages -> Source: main branch / root"
    )
    return True


if __name__ == "__main__":
    ok = deploy()
    sys.exit(0 if ok else 1)
