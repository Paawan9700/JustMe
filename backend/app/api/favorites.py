"""
Favourite voices:

    GET    /api/favorites                 - the user's favourites, newest first
    GET    /api/favorites/{favorite_id}   - one favourite (fresh sample URL)
    POST   /api/favorites                 - heart a speaker of a job awaiting selection
    PATCH  /api/favorites/{favorite_id}   - set / clear the user's own name for it
    DELETE /api/favorites/{favorite_id}   - remove it

Strictly per user: someone else's favourite is 404. See
services/favorite_service.py for how favourites are matched and learned.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response, status

from app.api.deps import get_current_user
from app.models.favorite import FavoriteCreateRequest, FavoriteRenameRequest, FavoriteResponse
from app.services import favorite_service
from app.services.storage import get_storage

router = APIRouter(prefix="/api/favorites", tags=["favorites"])


def _response(doc: dict[str, Any]) -> FavoriteResponse:
    sample_url = None
    if doc.get("sample_key"):
        sample_url = get_storage().get_presigned_url(
            doc["sample_key"], response_content_type="audio/mpeg", inline=True,
        )
    return FavoriteResponse(
        favorite_id=doc["favorite_id"],
        name=doc.get("name"),
        created_at=doc["created_at"],
        source_job_id=doc["source_job_id"],
        source_video_title=doc.get("source_video_title"),
        sample_url=sample_url,
        sample_count=1 + len(doc.get("learned_samples") or []),
    )


def _not_found() -> HTTPException:
    return HTTPException(status_code=404, detail="Favourite not found")


@router.get("", response_model=list[FavoriteResponse])
async def list_favorites(user: dict[str, Any] = Depends(get_current_user)) -> list[FavoriteResponse]:
    return [_response(d) for d in await favorite_service.list_favorites(user["user_id"])]


@router.get("/{favorite_id}", response_model=FavoriteResponse)
async def get_favorite(
    favorite_id: str, user: dict[str, Any] = Depends(get_current_user),
) -> FavoriteResponse:
    doc = await favorite_service.get_favorite(user["user_id"], favorite_id)
    if doc is None:
        raise _not_found()
    return _response(doc)


@router.post("", response_model=FavoriteResponse, status_code=status.HTTP_201_CREATED)
async def create_favorite(
    payload: FavoriteCreateRequest, user: dict[str, Any] = Depends(get_current_user),
) -> FavoriteResponse:
    try:
        doc = await favorite_service.create_favorite(user, payload.job_id, payload.speaker_label)
    except favorite_service.FavoriteError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
    return _response(doc)


@router.patch("/{favorite_id}", response_model=FavoriteResponse)
async def rename_favorite(
    favorite_id: str,
    payload: FavoriteRenameRequest,
    user: dict[str, Any] = Depends(get_current_user),
) -> FavoriteResponse:
    doc = await favorite_service.rename_favorite(user["user_id"], favorite_id, payload.name)
    if doc is None:
        raise _not_found()
    return _response(doc)


@router.delete("/{favorite_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_favorite(
    favorite_id: str, user: dict[str, Any] = Depends(get_current_user),
) -> Response:
    if not await favorite_service.delete_favorite(user["user_id"], favorite_id):
        raise _not_found()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
