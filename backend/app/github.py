from urllib.parse import parse_qs, quote, urlparse

import httpx
from fastapi import HTTPException, status

GITHUB_REPOS_URL = "https://api.github.com/user/repos"
GITHUB_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "devin-learning-oauth",
}
REAUTH_DETAIL = "Please re-authenticate with GitHub"


def _has_next_page(link_header: str | None) -> bool:
    if not link_header:
        return False
    for part in link_header.split(","):
        if 'rel="next"' in part:
            return True
    return False


def _page_from_link(link_header: str | None, rel: str) -> int | None:
    if not link_header:
        return None
    for part in link_header.split(","):
        if f'rel="{rel}"' not in part:
            continue
        start = part.find("<")
        end = part.find(">")
        if start == -1 or end == -1:
            continue
        query = parse_qs(urlparse(part[start + 1 : end]).query)
        page = query.get("page", [None])[0]
        return int(page) if page is not None else None
    return None


async def list_user_repos(
    github_access_token: str, *, page: int, per_page: int
) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            GITHUB_REPOS_URL,
            headers={
                **GITHUB_HEADERS,
                "Authorization": f"Bearer {github_access_token}",
            },
            params={
                "page": page,
                "per_page": per_page,
                "sort": "updated",
                "affiliation": "owner,collaborator,organization_member",
            },
        )
    _raise_for_github_response(response, fallback="GitHub repo list failed")

    raw = response.json()
    repos = [
        {
            "id": repo["id"],
            "full_name": repo["full_name"],
            "private": repo["private"],
            "html_url": repo["html_url"],
            "description": repo.get("description"),
            "default_branch": repo.get("default_branch"),
            "updated_at": repo.get("updated_at"),
        }
        for repo in raw
    ]
    link = response.headers.get("link")
    return {
        "repos": repos,
        "page": page,
        "per_page": per_page,
        "has_next": _has_next_page(link),
        "has_prev": page > 1,
        "next_page": _page_from_link(link, "next"),
        "prev_page": _page_from_link(link, "prev") if page > 1 else None,
    }


def _raise_for_github_response(
    response: httpx.Response, *, fallback: str
) -> None:
    if response.status_code in (401, 403) and _github_auth_failure(response):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=REAUTH_DETAIL,
        )
    if response.status_code >= 400:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=fallback,
        )


def _github_auth_failure(response: httpx.Response) -> bool:
    if response.status_code == 401:
        return True
    if response.status_code != 403:
        return False
    body = response.text.lower()
    return "bad credentials" in body or "requires authentication" in body


async def user_has_repo_access(github_access_token: str, full_name: str) -> bool:
    """Fresh check against GitHub's /user/repos list (paginated)."""
    page = 1
    while True:
        listing = await list_user_repos(
            github_access_token, page=page, per_page=100
        )
        for repo in listing["repos"]:
            if repo["full_name"].lower() == full_name.lower():
                return True
        if not listing["has_next"]:
            return False
        page = listing["next_page"] or (page + 1)


def public_github_url(full_name: str) -> str:
    return f"https://github.com/{full_name}.git"


def authenticated_clone_url(full_name: str, token: str) -> str:
    encoded = quote(token, safe="")
    return f"https://x-access-token:{encoded}@github.com/{full_name}.git"


def repo_full_name_from_url(repo_url: str) -> str:
    path = urlparse(repo_url).path.strip("/")
    if path.endswith(".git"):
        path = path[:-4]
    return path


async def github_repo_metadata(github_access_token: str, full_name: str) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(
            f"https://api.github.com/repos/{full_name}",
            headers={
                **GITHUB_HEADERS,
                "Authorization": f"Bearer {github_access_token}",
            },
        )
    _raise_for_github_response(response, fallback="GitHub repository lookup failed")
    data = response.json()
    return {
        "full_name": data.get("full_name", full_name),
        "default_branch": data.get("default_branch") or "main",
    }


async def create_github_pull_request(
    github_access_token: str,
    full_name: str,
    *,
    title: str,
    body: str,
    head: str,
    base: str,
) -> dict:
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            f"https://api.github.com/repos/{full_name}/pulls",
            headers={
                **GITHUB_HEADERS,
                "Authorization": f"Bearer {github_access_token}",
            },
            json={"title": title, "body": body, "head": head, "base": base},
        )
    _raise_for_github_response(response, fallback="GitHub pull request failed")
    data = response.json()
    return {
        "number": data.get("number"),
        "html_url": data.get("html_url"),
        "title": data.get("title"),
    }
