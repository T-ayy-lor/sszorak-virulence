# sszorak-virulence

Maps where each raider stood during Mythic Sszorak's Virulence against their assigned spot, plus damage done and Tempest hits per pull.

**Use:** open https://t-ayy-lor.github.io/sszorak-virulence/ and filter by cast, pull or player.

**How it's made:** an uploader script reads the WoW combat log (Advanced Combat Logging on) and pushes each night's data to `nights/` after each pull. The page is a single `index.html` that draws it over the boss room, one night at a time or all nights combined.
