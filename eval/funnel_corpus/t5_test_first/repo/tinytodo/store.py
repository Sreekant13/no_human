"""The store: items carry a due day, counted from the epoch day 0."""


class Store:
    """Items live in memory and are numbered from 1."""

    def __init__(self):
        self._items = []

    def add(self, title, due=None):
        """*due* is a day number (an int) or None for "no due date"."""
        self._items.append({"id": len(self._items) + 1, "title": title,
                            "done": False, "due": due})
        return self._items[-1]

    def complete(self, item_id):
        for item in self._items:
            if item["id"] == item_id:
                item["done"] = True
                return item
        return None

    def all(self):
        return list(self._items)
