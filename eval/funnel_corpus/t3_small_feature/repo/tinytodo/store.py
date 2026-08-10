"""The store: add items, list them, mark them done."""


class UnknownItem(KeyError):
    """Raised when an id is not in the store."""


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
        if item_id not in self._items:
            raise UnknownItem(item_id)
        self._items[item_id]["done"] = True
        return self._items[item_id]

    def all(self):
        return list(self._items.values())
