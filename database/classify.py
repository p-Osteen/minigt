import re
from typing import List, Tuple, Optional

CANONICAL_MAKERS = [
    "Alfa Romeo", "Aston Martin", "Land Rover", "Mercedes-Benz", "Western Star",
    "Acura", "Abarth", "Audi", "BMW", "Bentley", "Bugatti", "Cadillac", "Chevrolet",
    "Datsun", "Dodge", "Ducati", "Ferrari", "Ford", "Honda", "Hyundai", "Jaguar",
    "Lamborghini", "Lancia", "Lincoln", "Lotus", "Maserati", "Mazda", "McLaren",
    "Nissan", "Pagani", "Porsche", "RUF", "Shelby", "Subaru", "Toyota", "Volkswagen",
    "Tyrrell", "Sikorsky", "Isuzu", "Citroën", "Red Bull Racing", "AlphaTauri", 
    "Sauber", "Haas", "Williams", "Alpine"
]

MAKER_MAPPING = {
    "ferrari": "Ferrari",
    "subaru": "Subaru",
    "dodge": "Dodge",
    "porsche": "Porsche",
    "volkswagen": "Volkswagen",
    "vw": "Volkswagen",
    "mercedes": "Mercedes-Benz",
    "benz": "Mercedes-Benz",
    "amg": "Mercedes-Benz",
    "maybach": "Mercedes-Benz",
    "silverado": "Chevrolet",
    "corvette": "Chevrolet",
    "camaro": "Chevrolet",
    "eunos": "Mazda",
    "miata": "Mazda",
    "skyline": "Nissan",
    "fairlady": "Nissan",
    "silvia": "Nissan",
    "civic": "Honda",
    "nsx": "Honda",
    "integra": "Honda",
    "s2000": "Honda",
    "supra": "Toyota",
    "ae86": "Toyota",
    "trueno": "Toyota",
    "defender": "Land Rover",
    "range rover": "Land Rover",
    "mustang": "Ford",
    "bronco": "Ford",
    "isuzu": "Isuzu",
    "citroen": "Citroën",
    "citroën": "Citroën",
    "red bull": "Red Bull Racing",
    "alphatauri": "AlphaTauri",
    "sauber": "Sauber",
    "kick sauber": "Sauber",
    "haas": "Haas",
    "williams": "Williams",
    "alpine": "Alpine"
}

def is_cancelled_product(name: str, series: str, status: Optional[str]) -> bool:
    """Central check for cancelled or discontinued placeholder models."""
    n = (name or "").lower()
    s = (series or "").lower()
    st = (status or "").lower()
    for word in ["cancelled", "discontinued", "not presented", "cancelled model", "cancelled set"]:
        if word in n or word in s or word in st:
            return True
    
    cleaned_name = re.sub(r"\s+", " ", n).strip()
    if cleaned_name in ("rhd", "lhd"):
        return True
    return False

def get_manufacturers(name: str, brand: str, series: str) -> Tuple[Optional[str], List[str]]:
    """Scan name, brand, and series for makers, sorting by order of appearance."""
    found = []
    nb = f"{name} {brand} {series}".lower()
    
    for maker in CANONICAL_MAKERS:
        maker_lower = maker.lower()
        if maker_lower in nb:
            found.append(maker)
            
    for kw, maker in MAKER_MAPPING.items():
        if kw in nb and maker not in found:
            found.append(maker)
            
    unique_found = []
    for f in found:
        if f not in unique_found:
            unique_found.append(f)
            
    # Sort by position in 'brand + name'
    search_str = f"{brand} {name}".lower()
    def get_pos(maker):
        pos = search_str.find(maker.lower())
        if pos != -1:
            return pos
        for kw, m in MAKER_MAPPING.items():
            if m == maker:
                pos = search_str.find(kw)
                if pos != -1:
                    return pos
        return 999999
        
    unique_found.sort(key=get_pos)
    
    if not unique_found:
        if any(x in nb for x in ["container", "figurine", "pit box", "accessory", "accessories", "trailer"]):
            return "Accessories", ["Accessories"]
        return None, []
        
    primary = unique_found[0]
    return primary, unique_found



