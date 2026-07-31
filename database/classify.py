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
        if "hot wheels" in brand.lower() or "hotwheels" in brand.lower():
            return "Hot Wheels", ["Hot Wheels"]
        if "pop race" in brand.lower() or "poprace" in brand.lower():
            return "Pop Race", ["Pop Race"]
        return None, []
        
    primary = unique_found[0]
    return primary, unique_found


HW_GROUPS = {
    "Early Collections": [
        "Action Command", "Classics", "Classy Customs", "Drag Strippers", "Extras", 
        "Flying Colors", "Grand Prix", "HiRakers", "Megaforce", "Oldies But Goodies", 
        "Real Riders", "Rescue Team", "Speed Demons", "Speed Fleet", "Speed Machines", 
        "Speedway Specials", "Super Chromes", "Super Streeters", "The Heavies", 
        "The Heavys", "The Hot Ones", "The Spoilers", "Trailbusters", "Ultra Hots", "Workhorses"
    ],
    "Early Special Series": [
        "Action Packs", "Auto-City", "Automagic", "California Custom", "Chopcycles", 
        "Color Changers", "Color FX", "Convertables", "Crack-Ups", "Crashers", "Farbs", 
        "Fat Daddy Sizzlers", "Flip Outs", "Flippers", "Gran Toros", "Hot Birds", 
        "Hot Line", "Hot Shots", "Hot Wheels U.S.A.", "Hot Wheels World", 
        "Motorized X-V Racers", "Night Ridin' Sizzlers", "Planet Micro", "Revvers", 
        "RRRumblers", "Scorchers", "Shift Kickers", "Sizzlers", "Sizzlers II", 
        "Small Shots", "Steering Rigs", "Super California Custom", "Truck Co.", "X-V Racers", "Zowees"
    ],
    "Other Early Series": [
        "Action Cycles", "Action Racers", "Attack Pack", "Hot Wheels Railroad", "Key Force"
    ],
    "Modern Special Series": [
        "100%", "AcceleRacers", "Auto Affinity", "Battle Force 5", "Boulevard", "Car Culture", 
        "Character Cars", "Color Shifters", "Cool Classics", "Classics", "Delivery", 
        "Dragstrip Demons", "Fast & Furious Premium", "Flying Customs", "Formula One Collection", 
        "Hot Wheels Garage", "Hall of Fame", "High-Speed Wheels Track Stars", 
        "Highway 35 World Race", "Heritage", "Hot Wheels id", "Hot Wheels Racing", 
        "Mario Kart", "Nostalgic Brands", "Pop Culture", "Premium Collector Sets", 
        "Pro Racing", "Replica Entertainment", "Retro Style", "Since '68", 
        "Speed Machines", "Super Chromes", "Team Hot Wheels High-Speed Wheel", 
        "The Hot Ones", "Ultra Hots", "Vintage Racing"
    ],
    "Notable Modern Themed Assortments": [
        "50th Anniversary Favorites", "50th Anniversary Originals", "50th Anniversary Throwback", 
        "Pearl and Chrome Anniversary Series", "Batman", "Cars of the Decades", "Cop Rods", 
        "Easter Eggsclusives", "Easter", "Fast & Furious", "Fast & Furious Spy Racers", 
        "Fire Rods", "Fright Cars", "Halloween Cars", "Holiday Hot Rods", "HW Road Trippin'", 
        "HW Winter", "Neon Speeders", "Pantone", "Retro Style", "Spring", "Stars & Stripes", 
        "The Beatles Yellow Submarine", "Throwback", "Ultra Hots", "Vintage Racing Club"
    ],
    "Other Modern Series": [
        "1:87", "Atomix", "Battle X", "Custom Classics", "Custom Motors", "Dropstars", 
        "Extreme Shoxx", "Ferrari X-V", "G-Machines", "Hot Import Nights", "Hot Tunerz", 
        "Hot Wheels Haulers", "Hot Wheels Skate", "Lightyear", "Long Haulers", 
        "Modifighters", "Monster Jam", "Monster Trucks", "Moto Track Stars", "Motor Cycles", 
        "Pavement Pounders", "RacerVerse", "Racing Rigs", "Rapid Transit", "RC", 
        "Road Beasts", "Robo Wheels", "Shogun Racers", "Skate Freaks", "Sky Busters", 
        "Snap Rides", "Speed Cycles", "Speed Demons", "Starships", "Super Rigs", 
        "Superstar Speeders", "Thunder Cycles", "Track Fleet", "Track Stars Haulers", 
        "Trackin' Trucks", "Truckin' Transporters", "Volkswagen", "Wrecking Wheels"
    ],
    "Exclusives": [
        "Elite 64", "HWC.com", "Red Line Club", "Virtual Garage"
    ],
    "Larger Scale": [
        "1:18", "1:24", "100% 1:18", "1:43 Battle Vehicles", "1:43 Pull-Backs", "Batman 1:50", 
        "Classics 1:18", "Collectibles 1:18", "Ferrari 1:18", "Ferrari 1:24", "Ferrari 1:43", 
        "Formula Fuelers", "Hot Wheels Elite", "Hot Wheels Racing", "Hot Wheels XL", "La Storia", 
        "Let's Race: Activate!", "Passione", "Premium 1:43", "Pro Racing 1:43", 
        "Pull-Back Speeders", "Pullbax", "Street Power", "Tunerz", "Turbos Collection"
    ]
}


def classify_product(d: dict, toy_brand: str) -> dict:
    """Apply brand-specific taxonomy and categorization to product dictionary."""
    name = (d.get("product_name") or "").lower()
    series = (d.get("series") or "").lower()
    
    if toy_brand == "Hot Wheels":
        series_lower = (d.get("series") or "").lower()
        sub_series_lower = (d.get("sub_series") or "").lower()
        if any(x in series_lower or x in sub_series_lower for x in ["boulevard", "car culture", "team transport"]):
            d["series_line"] = "Premium"
        else:
            d["series_line"] = "Mainline"
            
        if "zamac" in name:
            d["finish"] = "Zamac"
        elif "super treasure hunt" in name or "super treasure hunt" in series_lower or "super treasure hunt" in sub_series_lower:
            d["finish"] = "Super Treasure Hunt"
        elif "treasure hunt" in name or "treasure hunt" in series_lower or "treasure hunt" in sub_series_lower:
            d["finish"] = "Treasure Hunt"
        else:
            d["finish"] = "Standard"

        # Hot Wheels Years Check (1968-2027)
        year_str = d.get("year")
        if year_str:
            try:
                y = int(year_str)
                if not (1968 <= y <= 2027):
                    d["year"] = None
            except ValueError:
                d["year"] = None

        # Hot Wheels Series Group classification
        series_group = "Miscellaneous"
        for group, names in HW_GROUPS.items():
            found = False
            for name_item in names:
                name_lower = name_item.lower()
                if (name_lower == series_lower or 
                    name_lower == sub_series_lower or 
                    name_lower in series_lower or 
                    name_lower in sub_series_lower):
                    series_group = group
                    found = True
                    break
            if found:
                break
        d["series_group"] = series_group
            
    elif toy_brand == "Pop Race":
        sub_series = "Regular"
        if "singer" in name or "singer" in series:
            sub_series = "Singer"
        elif "rwb" in name or "rwb" in series:
            sub_series = "RWB"
        elif "bape" in name or "bape" in series:
            sub_series = "BAPE"
        elif "eva" in name or "evangelion" in name:
            sub_series = "Evangelion Racing"
        d["sub_series"] = sub_series
        
        if "chrome" in name or "chrome" in series:
            d["finish"] = "Chrome"
        else:
            d["finish"] = "Standard"

        # Pop Race Collections
        series_str = (d.get("series") or "").strip()
        series_lower = series_str.lower()
        
        if "regular" in series_lower:
            collection = "Regular Collection"
        elif "enigma" in series_lower:
            collection = "Enigma"
        elif "event" in series_lower:
            collection = "Event Exclusives"
        elif "dark chrome" in series_lower:
            collection = "Dark Chrome Series"
        elif "ts exclusive" in series_lower:
            collection = "TS Exclusives"
        elif "blind box" in series_lower:
            collection = "Blind Box Series"
        elif "xcartoys" in series_lower:
            collection = "Xcartoys (china and all)"
        else:
            collection = series_str or "Regular Collection"
        d["collection"] = collection

        # Pop Race Regions & Inner Categories
        brand_val = (d.get("brand") or "").strip()
        manufacturer_val = (d.get("manufacturer") or "").strip()
        m_name = (manufacturer_val or brand_val).lower()
        
        japanese_brands = ["acura", "datsun", "honda", "isuzu", "mazda", "nissan", "subaru", "toyota"]
        european_brands = [
            "alfa romeo", "aston martin", "audi", "bmw", "bentley", "bugatti", "citroën", "citroen", 
            "ferrari", "jaguar", "lamborghini", "lancia", "land rover", "lotus", "maserati", "mclaren", 
            "mercedes-benz", "mercedes", "porsche", "ruf", "sauber", "volkswagen", "vw", "williams", "alpine"
        ]
        american_brands = ["cadillac", "chevrolet", "dodge", "ford", "haas", "lincoln", "shelby", "western star"]
        
        region = "Other"
        prod_name_lower = (d.get("product_name") or "").lower()
        tuner_keywords = ["rwb", "pandem", "spoon", "hks", "greddy", "liberty walk", "lb works", "tuner", "tuning"]
        
        if any(tk in prod_name_lower or tk in series_lower for tk in tuner_keywords):
            region = "Japanese Tuners"
        elif any(jb in m_name for jb in japanese_brands):
            region = "Japanese"
        elif any(eb in m_name for eb in european_brands):
            region = "European"
        elif any(ab in m_name for ab in american_brands):
            region = "American"
        d["region"] = region
            
    return d



