"""Authentication: passwords (bcrypt), JWTs (HS256), refresh sessions, ``get_current_user``.

Must not import ``backend.dependencies`` or ``backend.main`` (``backend.dependencies``
imports ``backend.auth.dependencies`` at top level). Settings and the database are read
from ``request.app.state`` or passed in explicitly.
"""

from backend.auth.repository import ensure_indexes

__all__ = ["ensure_indexes"]
