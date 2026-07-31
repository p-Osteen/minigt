from sqlalchemy import Column, String, Text, Index, Integer, Boolean
from sqlalchemy.orm import declarative_base
from sqlalchemy.ext.declarative import declared_attr
import json
from typing import List

Base = declarative_base()

class ProductMixin:
    """Shared fields and properties for all diecast brands."""
    item_number = Column(String, primary_key=True, index=True)
    product_name = Column(String, nullable=False, index=True)
    brand = Column(String, index=True, nullable=False)
    scale = Column(String, nullable=False, default="1:64", index=True)
    series = Column(String, nullable=True, index=True)
    sub_series = Column(String, nullable=True, index=True)
    images = Column(Text, nullable=True)  # JSON-encoded array of local paths/URLs
    source = Column(String, nullable=True, index=True)
    
    release_year = Column(Integer, nullable=True, index=True)
    release_year_confidence = Column(String, nullable=True)
    status = Column(String, nullable=True)
    is_cancelled = Column(Boolean, default=False, index=True)
    toy_brand = Column(String, nullable=False, index=True)

    @property
    def image_list(self) -> List[str]:
        if not self.images:
            return []
        try:
            return json.loads(self.images)
        except Exception:
            return []

    def set_images(self, paths: List[str]) -> None:
        self.images = json.dumps(paths, ensure_ascii=False)

    def to_dict(self) -> dict:
        return {
            "toy_brand": self.toy_brand,
            "item_number": self.item_number,
            "product_name": self.product_name,
            "brand": self.brand,
            "scale": self.scale,
            "series": self.series or "Regular",
            "sub_series": self.sub_series or "Regular",
            "images": self.image_list,
            "source": self.source,
            "release_year": self.release_year,
            "release_year_confidence": self.release_year_confidence,
            "status": self.status or "Released",
            "is_cancelled": bool(self.is_cancelled)
        }

class MiniGTProduct(Base, ProductMixin):
    __tablename__ = "minigt_products"

    @declared_attr
    def __table_args__(cls):
        return (
            Index(f"idx_{cls.__tablename__}_brand_series", "brand", "series"),
            Index(f"idx_{cls.__tablename__}_brand_scale", "brand", "scale"),
        )

class HotWheelsProduct(Base, ProductMixin):
    __tablename__ = "hotwheels_products"

    @declared_attr
    def __table_args__(cls):
        return (
            Index(f"idx_{cls.__tablename__}_brand_series", "brand", "series"),
            Index(f"idx_{cls.__tablename__}_brand_scale", "brand", "scale"),
        )

class PopRaceProduct(Base, ProductMixin):
    __tablename__ = "poprace_products"

    @declared_attr
    def __table_args__(cls):
        return (
            Index(f"idx_{cls.__tablename__}_brand_series", "brand", "series"),
            Index(f"idx_{cls.__tablename__}_brand_scale", "brand", "scale"),
        )

BRAND_MODELS = {
    "MINI GT": MiniGTProduct,
    "Hot Wheels": HotWheelsProduct,
    "Pop Race": PopRaceProduct,
}

def get_product_model(toy_brand: str):
    """Retrieves the specific product model class for a toy brand name."""
    brand_clean = toy_brand.lower()
    for k, model in BRAND_MODELS.items():
        if k.lower() in brand_clean:
            return model
    # Fallback to MiniGTProduct if brand is unrecognized
    return MiniGTProduct
