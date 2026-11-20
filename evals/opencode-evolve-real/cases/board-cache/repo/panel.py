from models import Snapshot
from preview import Preview


def draw(preview: Preview, snapshot: Snapshot, **options) -> str:
    bundle = preview.build(snapshot, **options)
    return "\n".join(bundle[snapshot.slug])
