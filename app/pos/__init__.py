"""POS integrations. Register new connectors in PROVIDERS."""
from typing import Dict, Type

from app.pos.base import PosAdapter, PosError
from app.pos.clover import CloverAdapter
from app.pos.generic import GenericAdapter
from app.pos.square import SquareAdapter

PROVIDERS: Dict[str, Type[PosAdapter]] = {a.provider: a for a in [SquareAdapter, CloverAdapter, GenericAdapter]}


def get_adapter_class(provider: str) -> Type[PosAdapter]:
    try:
        return PROVIDERS[provider]
    except KeyError:
        raise PosError(f"Unknown POS '{provider}'. Available: {', '.join(PROVIDERS)}.") from None
