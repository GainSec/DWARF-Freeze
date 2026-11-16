"""Drop droppable items whose payload has a fee above one million."""


def on_item(item):
    payload = item.get("payload")
    if (
        item.get("droppable")
        and isinstance(payload, dict)
        and isinstance(payload.get("fee"), (int, float))
        and not isinstance(payload["fee"], bool)
        and payload["fee"] > 1_000_000
    ):
        return "drop"
    return None
