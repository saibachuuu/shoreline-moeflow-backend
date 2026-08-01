"""Individual migration modules, ordered by their ``mNNNN_`` filename.

Every module must define ``VERSION``, ``NAME``, ``DESCRIPTION``, ``LEVEL`` and
the functions ``up(db)``, ``down(db)`` and ``verify(db)``.

``up`` MUST be idempotent
-------------------------
Moeflow runs on a standalone MongoDB, so there are no transactions to roll a
half-finished migration back. If ``up`` fails partway, or ``verify`` rejects the
result, the documents it already touched stay changed and no ``migration_record``
row is written -- the next run simply calls ``up`` again. Write every ``up`` so
that a second call over already-migrated data is a no-op:

- create indexes by an explicit ``name`` (re-creating an identical index is a
  no-op, but the same keys under a different name is an error);
- guard backfills with a filter that stops matching once applied, e.g.
  ``{"th": {"$exists": False}}`` rather than an unconditional ``update_many``.

``verify`` must confirm the end state, not the work performed, so that it also
passes when ``up`` had nothing left to do.
"""
