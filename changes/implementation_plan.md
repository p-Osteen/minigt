# Implementation Plan - Exclude Brands, Fix Image Loading & Add Pop Race/Hot Wheels Custom Filters

This plan details the changes needed to prevent non-Pop Race brands from being imported, resolve the missing/broken image display issues for both Hot Wheels and Pop Race, update crawler scope, and implement advanced brand-specific filtering in the frontend dashboard.

## User Review Required

> [!IMPORTANT]
> The new filters introduce custom classification properties (such as Pop Race collections, regions, and Hot Wheels series groups). These will be derived dynamically in the classification script and mapped in the UI. 

---

## Proposed Changes

### Crawler Module

#### [MODIFY] [crawler.py](file:///c:/Users/Paul/Desktop/Mods/TSM/crawler/crawler.py)
* **Pop Race Fandom Crawling**:
  * Ensure that the MediaWiki API (`https://pop-race.fandom.com/api.php?action=parse&page={page_name}&format=json&prop=text`) is thoroughly traversed by parsing all sub-links, tables, and nested categories recursively.
  * Update the exclusion check in `PopRaceBrandHandler.discover_sources` to ignore non-Pop Race brands such as `BM Creations`, `INNO64`, `MINI GT`, `PARA64`, `Tarmac Works`, and `unique model`.
* **Diecast Society Crawling**:
  * Expand the search page limit for Pop Race items from `1..3` to `1..15` (`https://diecastsociety.com/page/{page_idx}/?s=Pop+Race`).

---

### Classification System

#### [MODIFY] [classify.py](file:///c:/Users/Paul/Desktop/Mods/TSM/database/classify.py)
Update product classification to tag products with their respective filter values:
* **Pop Race Collections**:
  * Classify series/collection values into: `Regular Collection`, `Enigma`, `Event Exclusives`, `Dark Chrome Series`, `TS Exclusives`, `Blind Box Series`, `Xcartoys (china and all)`.
* **Pop Race Regions & Inner Categories**:
  * Map manufacturers to regions:
    * **Japanese**: Acura, Datsun, Honda, Isuzu, Mazda, Nissan, Subaru, Toyota.
    * **Japanese Tuners**: Japanese manufacturers featuring tuner specs (RWB, Pandem, Spoon, Spoon Sports, HKS, GReddy, Liberty Walk / LB Works, custom tuned models).
    * **European**: Alfa Romeo, Aston Martin, Audi, BMW, Bentley, Bugatti, Citroën, Ferrari, Jaguar, Lamborghini, Lancia, Land Rover, Lotus, Maserati, McLaren, Mercedes-Benz, Porsche, RUF, Sauber, Volkswagen, Williams, Alpine.
    * **American**: Cadillac, Chevrolet, Dodge, Ford, Haas, Lincoln, Shelby, Western Star.
* **Hot Wheels Years**:
  * Enforce year extraction mapping from 1968 to 2027.
* **Hot Wheels Series Groupings**:
  * Map Hot Wheels sub-series and series names to their respective historical groups:
    * **Early Collections**: Action Command, Classics, Classy Customs, Drag Strippers, Extras, Flying Colors, Grand Prix, HiRakers, Megaforce, Oldies But Goodies, Real Riders, Rescue Team, Speed Demons, Speed Fleet, Speed Machines, Speedway Specials, Super Chromes, Super Streeters, The Heavies, The Heavys, The Hot Ones, The Spoilers, Trailbusters, Ultra Hots, Workhorses.
    * **Early Special Series**: Action Packs, Auto-City, Automagic, California Custom, Chopcycles, Color Changers, Color FX, Convertables, Crack-Ups, Crashers, Farbs, Fat Daddy Sizzlers, Flip Outs, Flippers, Gran Toros, Hot Birds, Hot Line, Hot Shots, Hot Wheels U.S.A., Hot Wheels World, Motorized X-V Racers, Night Ridin' Sizzlers, Planet Micro, Revvers, RRRumblers, Scorchers, Shift Kickers, Sizzlers, Sizzlers II, Small Shots, Steering Rigs, Super California Custom, Truck Co., X-V Racers, Zowees.
    * **Other Early Series**: Action Cycles, Action Racers, Attack Pack, Hot Wheels Railroad, Key Force.
    * **Modern Special Series**: 100%, AcceleRacers, Auto Affinity, Battle Force 5, Boulevard, Car Culture, Character Cars, Color Shifters, Cool Classics, Classics, Delivery, Dragstrip Demons, Fast & Furious Premium, Flying Customs, Formula One Collection, Hot Wheels Garage, Hall of Fame, High-Speed Wheels Track Stars, Highway 35 World Race, Heritage, Hot Wheels id, Hot Wheels Racing, Mario Kart, Nostalgic Brands, Pop Culture, Premium Collector Sets, Pro Racing, Replica Entertainment, Retro Style, Since '68, Speed Machines, Super Chromes, Team Hot Wheels High-Speed Wheel, The Hot Ones, Ultra Hots, Vintage Racing.
    * **Notable Modern Themed Assortments**: 50th Anniversary Favorites, 50th Anniversary Originals, 50th Anniversary Throwback, Pearl and Chrome Anniversary Series, Batman, Cars of the Decades, Cop Rods, Easter Eggsclusives, Easter, Fast & Furious, Fast & Furious Spy Racers, Fire Rods, Fright Cars, Halloween Cars, Holiday Hot Rods, HW Road Trippin', HW Winter, Neon Speeders, Pantone, Retro Style, Spring, Stars & Stripes, The Beatles Yellow Submarine, Throwback, Ultra Hots, Vintage Racing Club.
    * **Other Modern Series**: 1:87, Atomix, Battle X, Custom Classics, Custom Motors, Dropstars, Extreme Shoxx, Ferrari X-V, G-Machines, Hot Import Nights, Hot Tunerz, Hot Wheels Haulers, Hot Wheels Skate, Lightyear, Long Haulers, Modifighters, Monster Jam, Monster Trucks, Moto Track Stars, Motor Cycles, Pavement Pounders, RacerVerse, Racing Rigs, Rapid Transit, RC, Road Beasts, Robo Wheels, Shogun Racers, Skate Freaks, Sky Busters, Snap Rides, Speed Cycles, Speed Demons, Starships, Super Rigs, Superstar Speeders, Thunder Cycles, Track Fleet, Track Stars Haulers, Trackin' Trucks, Truckin' Transporters, Volkswagen, Wrecking Wheels.
    * **Exclusives**: Elite 64, HWC.com, Red Line Club, Virtual Garage.
    * **Larger Scale**: 1:18, 1:24, 100% 1:18, 1:43 Battle Vehicles, 1:43 Pull-Backs, Batman 1:50, Classics 1:18, Collectibles 1:18, Ferrari 1:18, Ferrari 1:24, Ferrari 1:43, Formula Fuelers, Hot Wheels Elite, Hot Wheels Racing, Hot Wheels XL, La Storia, Let's Race: Activate!, Passione, Premium 1:43, Pro Racing 1:43, Pull-Back Speeders, Pullbax, Street Power, Tunerz, Turbos Collection.
    * **Miscellaneous**.

---

### Dashboard Web Interface

#### [MODIFY] [index.html](file:///c:/Users/Paul/Desktop/Mods/TSM/index.html)
* **Bypass Referrer Blocks**: Add `<meta name="referrer" content="no-referrer">` in the `<head>`.
* **Dynamic Filter Bar**:
  * Dynamically update/re-render the filter bar elements when switching between **MINI GT**, **Hot Wheels**, and **Pop Race**.
  * **For MINI GT**: Show the standard `Manufacturer`, `Series`, and `Year` selects.
  * **For Pop Race**:
    * Show a **Collection** filter (`All Collections`, `Regular Collection`, `Enigma`, `Event Exclusives`, `Dark Chrome Series`, `TS Exclusives`, `Blind Box Series`, `Xcartoys (china and all)`).
    * Show a **Region** filter (`All Regions`, `Japanese`, `Japanese Tuners`, `European`, `American`).
    * Update the **Manufacturer** dropdown to dynamically show inner categories (e.g. Acura, Honda, Nissan) based on the selected Region.
  * **For Hot Wheels**:
    * Show a **Year** filter listing years from 1968 to 2027.
    * Show a **Series Group** filter (`Early Collections`, `Early Special Series`, etc.).
    * Show a dynamic **Series** filter populated based on the selected Series Group.

#### [MODIFY] [catalog_print.html](file:///c:/Users/Paul/Desktop/Mods/TSM/catalog_print.html)
* Add `<meta name="referrer" content="no-referrer">` tag to allow high-resolution print images to render successfully.

---

## Verification Plan

### Automated Tests
- Compile and syntax check all Python files:
  ```powershell
  python -m py_compile crawler/crawler.py database/classify.py database/db_manager.py
  ```

### Manual Verification
- Clear database caches and run the updated crawler for Pop Race and Hot Wheels to verify all Fandom wiki details and the 15 Diecast Society pages crawl successfully.
- Verify that products are properly tagged with collections, regions, and series groups.
- Load `index.html` in the browser, switch brands, and test:
  * Brand switching dynamically displays the correct custom filters.
  * Selecting "Japanese" under Pop Race displays only Japanese manufacturers and filters models accordingly.
  * Filtering Hot Wheels by series group (e.g., "Modern Special Series") correctly shows the appropriate sub-series list and filters cards.
  * All images load without 403 Forbidden errors.
