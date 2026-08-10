# tinytodo

A very small in-memory todo list.

## Limits

A store holds at most 100 items. Adding one more raises `ListFull`.

## Usage

```python
from tinytodo.store import Store

s = Store()
s.add("buy milk")
```
