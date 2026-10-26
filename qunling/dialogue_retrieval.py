"""Use the existing semantic needs and overlap independent, bounded readers."""
import asyncio


def plan(understanding):
    if understanding is None:
        return {"profiles", "memory", "web_knowledge", "shared_knowledge"}  # Legacy frames.
    needs = set(understanding.information_needs)
    return {"profiles"} | ({"memory"} if "memory" in needs else set()) | (
        {"web_knowledge"} if "web" in needs else set()) | (
        {"shared_knowledge"} if needs & {"image", "file", "web", "history"} else set())


async def collect(understanding, readers, empty, *, timeout=3.2):
    selected = plan(understanding)

    async def read(name):
        try:
            async with asyncio.timeout(timeout):
                return name, await readers[name]()
        except Exception:
            return name, empty[name]

    result = dict(empty)
    result.update(await asyncio.gather(*(read(name) for name in readers if name in selected)))
    result["retrieval"] = {"requested": sorted(selected), "skipped": sorted(set(readers) - selected)}
    return result
