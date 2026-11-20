"""Assemble the overview from the application's report sections."""

from deskcore.application import Context
from deskcore.bulletins import visible
from deskcore.fields import Row
from deskcore.followups import due

from .operations import overview


def sections(ctx: Context, scope: str, day: str) -> Row:
    return {
        **overview(ctx, scope, day),
        "visible_bulletins": visible(ctx, scope, day),
        "followups": due(ctx, scope, day),
    }
