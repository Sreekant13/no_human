"""The store and its one hard limit."""

MAX_ITEMS = 200


class ListFull(RuntimeError):
    """Raised when the list already holds MAX_ITEMS items."""


class Store:
    """Items live in memory and are numbered from 1."""

    def __init__(self):
        self._items = []

    def add(self, title):
        if len(self._items) >= MAX_ITEMS:
            raise ListFull(MAX_ITEMS)
        self._items.append({"id": len(self._items) + 1, "title": title,
                            "done": False})
        return self._items[-1]

    def all(self):
        return list(self._items)
