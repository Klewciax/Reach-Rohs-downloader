"""Rejestr adapterów. Aby dodać producenta, zaimplementuj klasę dziedziczącą po BaseAdapter
i dopisz ją do ADAPTERS (szczegóły w README)."""
from __future__ import annotations

from .base import AdapterContext, BaseAdapter
from .generic import GenericAdapter
from .semis import (
    AnalogDevicesAdapter,
    InfineonAdapter,
    MicrochipAdapter,
    NexperiaAdapter,
    NXPAdapter,
    OnsemiAdapter,
    STMicroelectronicsAdapter,
    TexasInstrumentsAdapter,
)

ADAPTERS: dict[str, type[BaseAdapter]] = {
    cls.key: cls
    for cls in (
        GenericAdapter,
        TexasInstrumentsAdapter,
        AnalogDevicesAdapter,
        STMicroelectronicsAdapter,
        MicrochipAdapter,
        OnsemiAdapter,
        NexperiaAdapter,
        NXPAdapter,
        InfineonAdapter,
    )
}


def get_adapter(name: str) -> BaseAdapter:
    try:
        return ADAPTERS[name]()
    except KeyError:
        raise ValueError(f"Nieznany adapter '{name}'. Dostępne: {', '.join(sorted(ADAPTERS))}") from None


__all__ = ["ADAPTERS", "AdapterContext", "BaseAdapter", "get_adapter"]
