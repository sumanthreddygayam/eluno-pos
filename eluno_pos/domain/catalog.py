"""Frames and the lens matrix (type x index x coating) as sellable things."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Iterable, List, Optional, Tuple

from .errors import NotFound
from .optics import normalize_index, short_index

LENS_TYPES = ("single_vision", "bifocal", "progressive")


@dataclass(frozen=True)
class Frame:
    variant_id: int
    product_id: int
    sku: str
    title: str
    variant_title: str
    mrp: int
    hsn: str
    category: str  # optical | sunglasses
    rx_able: bool
    inventory_item_id: Optional[int]
    sellable: bool = True  # False for archived products: they appear in history, not at the till

    kind = "frame"

    @property
    def description(self) -> str:
        return ("%s %s" % (self.title, self.variant_title)).strip()


@dataclass(frozen=True)
class RxLimits:
    """The lab's constraints for one lens, in hundredths of a dioptre."""
    sph_min: int
    sph_max: int
    cyl_abs_max: int
    add_required: bool
    add_min: int
    add_max: int


@dataclass(frozen=True)
class Lens:
    variant_id: int
    product_id: int
    sku: str
    title: str
    lens_type: str
    index: str  # normalized, "1.60"
    coating: str
    mrp: int
    hsn: str
    limits: RxLimits
    lab_tat_days: int

    kind = "lens"
    inventory_item_id = None  # made to order, never stocked

    @property
    def config_code(self) -> str:
        """'progressive/1.6/blue_cut', the lens_config property format already used in Shopify."""
        return "%s/%s/%s" % (self.lens_type, short_index(self.index), self.coating)

    @property
    def label(self) -> str:
        return "%s %s %s" % (self.lens_type.replace("_", " ").title(), self.index, self.coating.replace("_", " "))

    def description(self, rx_id: Optional[str]) -> str:
        base = "%s %s %s lens" % (self.lens_type.replace("_", " ").title(), self.index, self.coating.replace("_", " "))
        return base + (", Rx %s" % rx_id if rx_id else "")


@dataclass(frozen=True)
class LensChoice:
    lens_type: str
    index: str
    coating: str

    @classmethod
    def parse(cls, text: str) -> "LensChoice":
        """'progressive/1.60/blue_cut' -> LensChoice."""
        parts = [p.strip() for p in text.split("/")]
        if len(parts) != 3:
            raise ValueError("lens must be type/index/coating, e.g. progressive/1.60/blue_cut")
        return cls(parts[0], normalize_index(parts[1]), parts[2])


class Catalog:
    def __init__(self, frames: Iterable[Frame], lenses: Iterable[Lens]):
        self.frames: Dict[str, Frame] = {f.sku: f for f in frames}
        self.frames_by_variant: Dict[int, Frame] = {f.variant_id: f for f in self.frames.values()}
        self.lenses: Dict[Tuple[str, str, str], Lens] = {(l.lens_type, l.index, l.coating): l for l in lenses}
        self.lenses_by_variant: Dict[int, Lens] = {l.variant_id: l for l in self.lenses.values()}

    def frame(self, sku: str) -> Frame:
        f = self.frames.get(sku)
        if not f:
            raise NotFound("No frame with SKU %s in the catalog." % sku, code="frame_not_found")
        return f

    def lens(self, choice: LensChoice) -> Lens:
        l = self.lenses.get((choice.lens_type, choice.index, choice.coating))
        if l:
            return l
        types = sorted({k[0] for k in self.lenses})
        indices = sorted({k[1] for k in self.lenses if k[0] == choice.lens_type})
        coatings = sorted({k[2] for k in self.lenses if k[0] == choice.lens_type})
        raise NotFound("No %s/%s/%s lens in the matrix. Types: %s; indices for %s: %s; coatings: %s." % (
            choice.lens_type, choice.index, choice.coating, ", ".join(types), choice.lens_type,
            ", ".join(indices) or "none", ", ".join(coatings) or "none"), code="lens_not_found")

    def lens_family(self, lens_type: str, coating: str) -> List[Lens]:
        """Same type and coating across indices, thinnest last."""
        return sorted((l for (t, _, c), l in self.lenses.items() if t == lens_type and c == coating),
                      key=lambda l: Decimal(l.index))
