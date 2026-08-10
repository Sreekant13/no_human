"""Turning items into something a human reads."""


def as_text(items):
    """One line per item: `1  [ ] buy milk`."""
    lines = []
    for item in items:
        box = "[x]" if item["done"] else "[ ]"
        lines.append(f"{item['id']}  {box} {item['title']}")
    return "\n".join(lines)
