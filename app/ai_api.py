"""REST endpoints for managing the assistant's AI providers (owner only), under /api/ai."""
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import ai
from app.agent_tools import TOOL_SCHEMAS
from app.db import get_db
from app.deps import owner_only
from app.models import AiProvider, User

router = APIRouter(prefix="/ai", tags=["AI providers"])
READ_ONLY_TOOLS = [t for t in TOOL_SCHEMAS if t["function"]["name"] in ("list_low_stock", "check_stock", "inventory_report")]


class ProviderIn(BaseModel):
    preset: str
    name: Optional[str] = Field(default=None, max_length=120)
    api_key: str = Field(default="", max_length=500)
    model: str = Field(default="", max_length=200)
    base_url: str = Field(default="", max_length=300)


class ProviderUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=120)
    api_key: Optional[str] = Field(default=None, max_length=500)  # blank keeps the saved key
    model: Optional[str] = Field(default=None, min_length=1, max_length=200)
    base_url: Optional[str] = Field(default=None, max_length=300)
    enabled: Optional[bool] = None


class OrderIn(BaseModel):
    ids: List[int]


def _mask(key: Optional[str]) -> str:
    if not key:
        return ""
    return "•••• " + key[-4:] if len(key) > 8 else "••••"


def _aware(dt):
    return dt.replace(tzinfo=timezone.utc) if dt is not None and dt.tzinfo is None else dt


def serialize(p: AiProvider) -> dict:
    preset = ai.PRESETS.get(p.preset, ai.PRESETS["custom"])
    until = _aware(p.cooldown_until)
    resting = until is not None and until > datetime.now(timezone.utc)
    return {"id": p.id, "preset": p.preset, "preset_label": preset["label"], "name": p.name, "base_url": p.base_url,
            "model": p.model, "api_key": _mask(p.api_key), "priority": p.priority, "enabled": p.enabled,
            "last_ok": p.last_ok, "last_status": p.last_status, "last_used_at": p.last_used_at,
            "resting_until": until if resting else None}


def _get(db: Session, pid: int) -> AiProvider:
    p = db.get(AiProvider, pid)
    if p is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Provider not found.")
    return p


def _config(p: AiProvider) -> ai.ProviderConfig:
    return ai.ProviderConfig(p.id, p.preset, p.name, p.base_url, p.api_key or "", p.model)


def _error_text(exc: Exception) -> str:
    return ai.classify(exc)[2]


@router.get("/presets")
def presets(_: User = Depends(owner_only)):
    return [{"preset": k, **v, "needs_key": k not in ai.NO_KEY_NEEDED} for k, v in ai.PRESETS.items()]


@router.get("/providers")
def list_providers(db: Session = Depends(get_db), _: User = Depends(owner_only)):
    return [serialize(p) for p in db.scalars(select(AiProvider).order_by(AiProvider.priority, AiProvider.id))]


@router.post("/providers", status_code=201)
def add_provider(body: ProviderIn, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    preset = ai.PRESETS.get(body.preset)
    if preset is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Unknown provider type.")
    base_url = (body.base_url.strip() or preset["base_url"]).strip()
    model = body.model.strip() or preset["model"]
    if not base_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter the service's base URL.")
    if not body.api_key.strip() and body.preset not in ai.NO_KEY_NEEDED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Paste your API key.")
    if not model:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Choose a model: press Load models or type its name.")
    last = db.scalar(select(func.max(AiProvider.priority))) or 0
    p = AiProvider(preset=body.preset, name=(body.name or preset["label"]).strip(), base_url=base_url,
                   api_key=body.api_key.strip() or None, model=model, priority=last + 1)
    db.add(p)
    db.commit()
    db.refresh(p)
    return serialize(p)


@router.patch("/providers/{pid}")
def update_provider(pid: int, body: ProviderUpdate, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    p = _get(db, pid)
    if body.name is not None:
        p.name = body.name.strip()
    if body.api_key and not body.api_key.startswith("••••"):
        p.api_key = body.api_key.strip()
    if body.model is not None:
        p.model = body.model.strip()
    if body.base_url:
        p.base_url = body.base_url.strip()
    if body.enabled is not None:
        p.enabled = body.enabled
        if body.enabled:
            p.cooldown_until = None
    db.commit()
    return serialize(p)


@router.post("/providers/order")
def reorder(body: OrderIn, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    for i, pid in enumerate(body.ids, start=1):
        p = db.get(AiProvider, pid)
        if p:
            p.priority = i
    db.commit()
    return [serialize(p) for p in db.scalars(select(AiProvider).order_by(AiProvider.priority, AiProvider.id))]


@router.delete("/providers/{pid}", status_code=204)
def delete_provider(pid: int, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    db.delete(_get(db, pid))
    db.commit()


@router.post("/providers/{pid}/test")
def test_provider(pid: int, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    p = _get(db, pid)
    try:
        ok, message = ai.probe_tools(_config(p), READ_ONLY_TOOLS)
    except Exception as exc:
        ok, message = False, _error_text(exc)
    p.last_ok, p.last_status = ok, message[:300]
    if ok:
        p.cooldown_until = None
    db.commit()
    return {"ok": ok, "message": message}


class ModelsIn(BaseModel):
    preset: str = "custom"
    base_url: str = ""
    api_key: str = ""
    provider_id: Optional[int] = None  # use this saved provider's key if api_key is blank


@router.post("/models")
def list_models(body: ModelsIn, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    """List the models a provider offers, so the owner can pick one (free ones first)."""
    saved = _get(db, body.provider_id) if body.provider_id else None
    preset = ai.PRESETS.get(body.preset, ai.PRESETS["custom"])
    key = body.api_key.strip()
    if (not key or key.startswith("••••")) and saved:
        key = saved.api_key or ""
    base_url = body.base_url.strip() or (saved.base_url if saved else "") or preset["base_url"]
    if not base_url:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Enter the service's base URL first.")
    if not key and body.preset not in ai.NO_KEY_NEEDED:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Paste your API key first.")
    try:
        client = ai.default_client_factory(ai.ProviderConfig(0, body.preset, "", base_url, key, ""))
        ids = sorted({m.id for m in client.models.list()})
    except Exception as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Couldn't load models: {_error_text(exc)}") from exc
    ids = [i[len("models/"):] if i.startswith("models/") else i for i in ids]  # Gemini prefixes ids
    ids.sort(key=lambda i: (not i.endswith(":free"), i))  # OpenRouter free models first
    return {"models": ids}
