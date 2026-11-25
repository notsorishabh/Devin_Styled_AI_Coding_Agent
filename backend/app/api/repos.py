from fastapi import APIRouter, Depends, Query

from app.deps import get_current_user
from app.github import list_user_repos
from app.models import User
from app.security import decrypt_github_token

router = APIRouter(tags=["repos"])


@router.get("/repos")
async def list_repos(
    user: User = Depends(get_current_user),
    page: int = Query(default=1, ge=1),
    per_page: int = Query(default=30, ge=1, le=100),
):
    token = decrypt_github_token(user.github_access_token)
    return await list_user_repos(token, page=page, per_page=per_page)
