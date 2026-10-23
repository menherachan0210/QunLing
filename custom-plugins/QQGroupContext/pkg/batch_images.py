"""Use the shared frozen-batch resolver with the vision reader URL policy."""
from qunling.group_batch import merge_history
from pkg.media import validate_url


async def merge_batch_images(plugin, context, history):
    return await merge_history(plugin, context, history, validate_url)
