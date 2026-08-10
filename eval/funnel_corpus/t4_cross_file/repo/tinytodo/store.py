"""The store: add items and list them."""


class Store:
    """Items live in memory and are numbered from 1."""

    def __init__(self):
        self._items = []

    def add(self, title):
        self._items.append({"id": len(self._items) + 1, "title": title,
                            "done": False})
        return self._items[-1]

    def all(self):
        return list(self._items)
