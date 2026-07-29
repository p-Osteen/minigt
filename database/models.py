from sqlalchemy import Column, String, Text, Index
from sqlalchemy.orm import declarative_base
import json
from typing import List

Base = declarative_base()

class Product(Base):
    """
    SQLAlchemy model representing a MINI GT 1:64 Scale model.
    """
    __tablename__ = "products"

    item_number = Column(String, primary_key=True, index=True)
    product_name = Column(String, nullable=False, index=True)
    brand = Column(String, index=True, nullable=False)
    scale = Column(String, nullable=False, default="1:64", index=True)
    series = Column(String, nullable=True, index=True)
    images = Column(Text, nullable=True)  # JSON-encoded array of local paths, e.g., ["images/Brand/MGT00123_0.jpg"]
    source = Column(String, nullable=True, index=True)

    # Explicit indexes for combined queries and fast searches
    __table_args__ = (
        Index("idx_brand_series", "brand", "series"),
        Index("idx_brand_scale", "brand", "scale"),
    )

    def to_dict(self) -> dict:
        """
        Converts Product instance to a dictionary for JSON output.
        """
        return {
            "item_number": self.item_number,
            "product_name": self.product_name,
            "brand": self.brand,
            "scale": self.scale,
            "series": self.series or "Regular",
            "images": self.image_list,
            "source": self.source
        }

    @property
    def image_list(self) -> List[str]:
        """Returns the list of local image paths as a Python list."""
        if not self.images:
            return []
        try:
            return json.loads(self.images)
        except Exception:
            return []

    def set_images(self, paths: List[str]) -> None:
        """Sets the images list by encoding to JSON."""
        self.images = json.dumps(paths, ensure_ascii=False)
