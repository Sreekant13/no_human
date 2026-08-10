"""The store: add items, list them, mark them done."""


class Store:
    """Items live in memory and are numbered from 1."""

    def __init__(self):
        self._items = {}
        self._next_id = 1

    def add(self, title):
        item = {"id": self._next_id, "title": title, "done": False}
        self._items[item["id"]] = item
        self._next_id += 1
        return item

    def complete(self, item_id):
        item = self._items.get(item_id)
        if item is None:
            return None
        item["done"] = True
        return item

    def all(self):
        return list(self._items.values())
