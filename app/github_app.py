"""GitHub App client: JWT -> installation token -> PR diff, review comments, Checks API."""
import time
import logging
import jwt
import httpx

from app.config import settings

log = logging.getLogger("hermesqa.github")
API = "https://api.github.com"


class GitHubClient:
    def __init__(self, installation_id: int):
        self.installation_id = installation_id
        self._token: str | None = None
        self._token_exp = 0.0

    # ---------- auth ----------
    def _app_jwt(self) -> str:
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": settings.github_app_id},
            settings.private_key,
            algorithm="RS256",
        )

    def token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        r = httpx.post(
            f"{API}/app/installations/{self.installation_id}/access_tokens",
            headers={"Authorization": f"Bearer {self._app_jwt()}", "Accept": "application/vnd.github+json"},
            timeout=30,
        )
        r.raise_for_status()
        self._token = r.json()["token"]
        self._token_exp = time.time() + 3600
        return self._token

    def _headers(self, accept="application/vnd.github+json") -> dict:
        return {"Authorization": f"Bearer {self.token()}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}

    # ---------- read ----------
    def get_pr_diff(self, owner: str, repo: str, pr: int) -> str:
        r = httpx.get(f"{API}/repos/{owner}/{repo}/pulls/{pr}", headers=self._headers("application/vnd.github.v3.diff"), timeout=60)
        r.raise_for_status()
        return r.text

    def get_pr_files(self, owner: str, repo: str, pr: int) -> list[dict]:
        files, page = [], 1
        while True:
            r = httpx.get(f"{API}/repos/{owner}/{repo}/pulls/{pr}/files", headers=self._headers(), params={"per_page": 100, "page": page}, timeout=60)
            r.raise_for_status()
            batch = r.json()
            files += batch
            if len(batch) < 100:
                return files
            page += 1

    # ---------- write ----------
    def create_check_run(self, owner: str, repo: str, head_sha: str, name="HermesQA Review") -> int:
        r = httpx.post(f"{API}/repos/{owner}/{repo}/check-runs", headers=self._headers(),
                       json={"name": name, "head_sha": head_sha, "status": "in_progress"}, timeout=30)
        r.raise_for_status()
        return r.json()["id"]

    def complete_check_run(self, owner: str, repo: str, check_id: int, conclusion: str, title: str, summary: str, text: str = ""):
        r = httpx.patch(f"{API}/repos/{owner}/{repo}/check-runs/{check_id}", headers=self._headers(),
                        json={"status": "completed", "conclusion": conclusion,
                              "output": {"title": title, "summary": summary[:65000], "text": text[:65000]}}, timeout=30)
        r.raise_for_status()

    def post_review(self, owner: str, repo: str, pr: int, head_sha: str, body: str, comments: list[dict], event="COMMENT"):
        """comments: [{path, line, side:'RIGHT', body}] - dùng `line` (API mới) thay `position`."""
        payload = {"commit_id": head_sha, "body": body, "event": event, "comments": comments}
        r = httpx.post(f"{API}/repos/{owner}/{repo}/pulls/{pr}/reviews", headers=self._headers(), json=payload, timeout=60)
        if r.status_code == 422:
            # Thường do line không nằm trong diff -> gửi lại chỉ phần summary
            log.warning("422 posting inline comments, fallback to summary only: %s", r.text[:300])
            payload["comments"] = []
            r = httpx.post(f"{API}/repos/{owner}/{repo}/pulls/{pr}/reviews", headers=self._headers(), json=payload, timeout=60)
        r.raise_for_status()
        return r.json()
