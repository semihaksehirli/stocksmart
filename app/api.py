"""FastAPI REST API for StockSmart.

Run:  uvicorn app.api:app --reload      (docs at http://localhost:8000/docs)
"""
from typing import List, Optional
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, status
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app import agent_tools, auth, services
from app.pos import sync as pos_sync
from app.db import get_db, init_db
from app.deps import current_user, owner_only
from app.models import MovementType, Role, User

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="StockSmart API", version="0.1.0", description="Small Business Inventory System", lifespan=lifespan)


# ---------- Schemas ----------

class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str


class SupplierIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    email: Optional[str] = None
    phone: Optional[str] = None


class SupplierOut(SupplierIn):
    model_config = ConfigDict(from_attributes=True)
    id: int


class ProductIn(BaseModel):
    sku: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=120)
    category: str = "General"
    price: float = Field(ge=0)
    cost: float = Field(ge=0)
    quantity: int = Field(default=0, ge=0)
    reorder_level: int = Field(default=5, ge=0)
    supplier_id: Optional[int] = None


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    price: Optional[float] = Field(default=None, ge=0)
    cost: Optional[float] = Field(default=None, ge=0)
    reorder_level: Optional[int] = Field(default=None, ge=0)
    supplier_id: Optional[int] = None


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    sku: str
    name: str
    category: str
    price: float
    cost: float
    quantity: int
    reorder_level: int
    supplier_id: Optional[int]
    is_low_stock: bool


class MovementIn(BaseModel):
    product_id: int
    type: MovementType
    quantity: int = Field(description="Positive for sale/delivery; signed for adjustment")
    note: Optional[str] = None


class ChatTurn(BaseModel):
    role: str
    content: str


class AssistantIn(BaseModel):
    message: str = Field(min_length=1, max_length=1000)
    history: List[ChatTurn] = Field(default_factory=list, max_length=20)  # earlier turns, for follow-ups


class AssistantOut(BaseModel):
    reply: str
    mode: str  # "ai" or "built-in"
    provider: Optional[str] = None
    model: Optional[str] = None
    skipped: List[str] = []  # providers that couldn't answer this time


class MeOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    role: str


class MovementOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    type: str
    change: int
    quantity_after: int
    note: Optional[str]
    created_at: datetime


# Auth dependencies live in app/deps.py (shared with the POS routes).


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except services.InventoryError as exc:
        code = status.HTTP_404_NOT_FOUND if "not found" in str(exc) else status.HTTP_400_BAD_REQUEST
        raise HTTPException(code, str(exc)) from exc


# ---------- Routes ----------

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/auth/login", response_model=TokenOut)
def login(body: LoginIn, db: Session = Depends(get_db)):
    user = auth.authenticate(db, body.username, body.password)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong username or password")
    return TokenOut(access_token=auth.create_token(user), role=user.role)


@app.get("/auth/me", response_model=MeOut)
def me(user: User = Depends(current_user)):
    return user


@app.get("/suppliers", response_model=list[SupplierOut])
def list_suppliers(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return services.list_suppliers(db)


@app.post("/suppliers", response_model=SupplierOut, status_code=201)
def create_supplier(body: SupplierIn, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    return _guard(services.create_supplier, db, **body.model_dump())


@app.get("/products", response_model=list[ProductOut])
def list_products(q: str = "", category: Optional[str] = None, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return services.search_products(db, q, category)


@app.post("/products", response_model=ProductOut, status_code=201)
def create_product(body: ProductIn, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    return _guard(services.create_product, db, **body.model_dump())


@app.get("/products/{product_id}", response_model=ProductOut)
def get_product(product_id: int, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return _guard(services.get_product, db, product_id)


@app.patch("/products/{product_id}", response_model=ProductOut)
def update_product(product_id: int, body: ProductUpdate, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    return _guard(services.update_product, db, product_id, **body.model_dump(exclude_unset=True))


@app.delete("/products/{product_id}", status_code=204)
def delete_product(product_id: int, db: Session = Depends(get_db), _: User = Depends(owner_only)):
    _guard(services.delete_product, db, product_id)


@app.post("/movements", response_model=MovementOut, status_code=201)
def create_movement(body: MovementIn, background: BackgroundTasks, db: Session = Depends(get_db),
                    user: User = Depends(current_user)):
    if body.type is MovementType.ADJUSTMENT and user.role != Role.OWNER.value:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only owners can make adjustments")
    movement = _guard(services.record_movement, db, body.product_id, body.type, body.quantity, body.note)
    # Keep connected POS systems in step (only those with "push stock" turned on).
    background.add_task(pos_sync.push_products_background, [movement.product_id])
    return movement


@app.get("/movements", response_model=list[MovementOut])
def list_movements(product_id: Optional[int] = None, limit: int = 100, db: Session = Depends(get_db), _: User = Depends(current_user)):
    return services.list_movements(db, product_id, limit)


@app.get("/alerts/low-stock", response_model=list[ProductOut])
def low_stock(db: Session = Depends(get_db), _: User = Depends(current_user)):
    return services.low_stock(db)


@app.get("/reports/dashboard")
def dashboard(db: Session = Depends(get_db), _: User = Depends(current_user)):
    summary = services.dashboard_summary(db)
    summary["top_sellers"] = [{"sku": p.sku, "name": p.name, "units_sold": u} for p, u in services.top_sellers(db)]
    return summary


@app.get("/reports/inventory.csv", response_class=PlainTextResponse)
def inventory_csv(db: Session = Depends(get_db), _: User = Depends(owner_only)):
    return PlainTextResponse(
        services.inventory_csv(db), media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=inventory.csv"},
    )


@app.post("/assistant", response_model=AssistantOut)
def assistant(body: AssistantIn, _: User = Depends(current_user)):
    """Ask the inventory assistant. Tries each AI provider in order, then built-in mode."""
    result = agent_tools.answer_full(body.message, [t.model_dump() for t in body.history])
    return AssistantOut(
        reply=result.reply, mode="ai" if result.provider else "built-in", provider=result.provider, model=result.model,
        skipped=[f"{a.name}: {a.message}" for a in result.attempts if a.outcome != "ok"],
    )


# POS integrations (Square, Clover, any other POS): see app/pos_api.py
from app.pos_api import router as pos_router  # noqa: E402

app.include_router(pos_router)

# AI providers for the assistant: see app/ai_api.py
from app.ai_api import router as ai_router  # noqa: E402

app.include_router(ai_router)
