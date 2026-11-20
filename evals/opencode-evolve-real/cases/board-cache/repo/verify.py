from models import Item, Snapshot
from panel import draw
from preview import Preview

preview = Preview()
assert draw(preview, Snapshot("west", 4, (Item("West", "main"),))) == "West"
assert draw(preview, Snapshot("east", 4, (Item("East", "main"),))) == "East"
