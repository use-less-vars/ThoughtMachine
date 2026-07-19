"""prompt_routes.py — CRUD endpoints for ~/.thoughtmachine/*.txt prompt files.

Allows the frontend to list, read, create/update, and delete system prompt
files stored in the user's .thoughtmachine directory.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

router = APIRouter(prefix="/api/prompts")

# ── Helpers ──────────────────────────────────────────────────────────────────

_PROMPT_DIR = Path.home() / ".thoughtmachine"


def _ensure_prompt_dir() -> Path:
    """Create ~/.thoughtmachine/ if it doesn't exist and return the path."""
    _PROMPT_DIR.mkdir(parents=True, exist_ok=True)
    return _PROMPT_DIR


def _list_txt_files() -> list[dict]:
    """Return a list of {name, size_bytes, modified_at} for every .txt file."""
    prompt_dir = _ensure_prompt_dir()
    results: list[dict] = []
    for child in sorted(prompt_dir.iterdir()):
        if child.is_file() and child.suffix.lower() == ".txt":
            stat = child.stat()
            results.append({
                "name": child.name,
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            })
    return results


def _resolve_filename(filename: str) -> Path:
    """Resolve a filename to an absolute path inside ~/.thoughtmachine/.

    Raises HTTPException 400 if the name is empty or contains path separators.
    Raises HTTPException 404 if the file does not exist (caller can check).
    """
    if not filename or not filename.strip():
        raise HTTPException(status_code=400, detail="Filename must not be empty.")

    # Prevent directory traversal
    if os.sep in filename or "/" in filename or "\\" in filename:
        raise HTTPException(
            status_code=400,
            detail="Filename must not contain path separators.",
        )

    # Ensure .txt extension for consistency
    if not filename.endswith(".txt"):
        filename += ".txt"

    return _ensure_prompt_dir() / filename


# ── Endpoints ────────────────────────────────────────────────────────────────


@router.get("")
async def list_prompts():
    """List all .txt files in ~/.thoughtmachine/."""
    return {"prompts": _list_txt_files()}


@router.get("/{filename}")
async def get_prompt(filename: str):
    """Read the content of a single prompt file."""
    path = _resolve_filename(filename)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Prompt file '{path.name}' not found.",
        )
    content = path.read_text(encoding="utf-8")
    stat = path.stat()
    return {
        "name": path.name,
        "content": content,
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
    }


@router.post("/{filename}")
async def save_prompt(filename: str, body: dict):
    """Create or overwrite a prompt file.

    Request body must contain a ``content`` key with the file content.
    """
    content = body.get("content")
    if content is None:
        raise HTTPException(
            status_code=400,
            detail="Request body must contain a 'content' field.",
        )

    path = _resolve_filename(filename)
    path.write_text(str(content), encoding="utf-8")
    stat = path.stat()
    return {
        "name": path.name,
        "content": str(content),
        "size_bytes": stat.st_size,
        "modified_at": stat.st_mtime,
    }


@router.delete("/{filename}")
async def delete_prompt(filename: str):
    """Delete a prompt file."""
    path = _resolve_filename(filename)
    if not path.exists():
        raise HTTPException(
            status_code=404,
            detail=f"Prompt file '{path.name}' not found.",
        )
    path.unlink()
    return {"deleted": path.name}
