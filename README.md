# Taynalysis

Raid-progression analysis for World of Warcraft, built from the combat log.

**Pages:**
- `index.html`: Mythic Sszorak. Where each raider stood during Virulence against their assigned spot, plus damage done and Tempest hits per pull.
- `fangs.html`: Mythic Twin Fangs. Eternal Venom sources, Caustic Globule CC and soaks, Rouse the Brood kicks, Ravenous Feast soaks, meters and early deaths.

**Use:** open https://t-ayy-lor.github.io/taynalysis/

**How it's made:** `taynalysis_uploader.py` reads the WoW combat log (Advanced Combat Logging on) and uploads each night's data after every pull. The pages draw it, one night at a time or all nights combined.
