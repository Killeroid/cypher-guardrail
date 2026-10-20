"""What a validator or a snapshot can be given to talk to a database.

FalkorDBLite's client is a ``falkordb.FalkorDB``, so it is a :data:`Driver` too.
"""

from typing import Literal

import falkordb
from falkordb.asyncio import FalkorDB as AsyncFalkorDB
from falkordb.asyncio.graph import AsyncGraph

#: A ``falkordb.FalkorDB`` client (or FalkorDBLite), or one of its graphs (``client.select_graph(name)``).
Driver = falkordb.FalkorDB | falkordb.Graph

#: A ``falkordb.asyncio.FalkorDB`` client, or one of its graphs.
AsyncDriver = AsyncFalkorDB | AsyncGraph

#: What a hybrid validator does with a thing that its snapshot says is not there: ask the database (``"misses"``, the default) or trust the snapshot.
Verify = Literal["misses", "never"]
