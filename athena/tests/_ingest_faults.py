"""Fault injection for the « incertain » lot (2026-11-06): a document
record whose commit ANSWER is lost, and a store that then cannot read it.

Shared by the tests of every caller of the document ingestion — the model,
the upload page, Réception, the receipt copy, the generations and the
connector — so each plays the SAME failure on the shared fake Firestore
(``tests/_fake_firestore.py``). The fake sits below the GAPIC retry
wrapper, so the client's own retry of a lost commit is played here,
verbatim (``retry_answers_exists``).
"""

from google.api_core.exceptions import AlreadyExists


def _record_id(server, write) -> str:
    """The id of a top-level ``documents/{id}`` record *write* creates or
    updates, else ``""`` (a subcollection entry, another collection)."""
    rel = server.rel(server._write_name(getattr(write, "_pb", write)))
    parts = rel.split("/")
    if len(parts) == 2 and parts[0] == "documents":
        return parts[1]
    return ""


def lose_answer_then_unreadable(fake, monkeypatch, *, land=True, skip_ids=(),
                                retry_answers_exists=False) -> dict:
    """The first commit writing a ``documents/{id}`` record (``id`` not in
    *skip_ids*) LANDS — or not, with ``land=False`` — then RAISES, as a
    connection reset after (or before) the server's commit would; with
    *retry_answers_exists* it lands and the client's retry of it is
    answered ``AlreadyExists``. From then on every read of that record
    fails, until the test sets ``state["dead"] = None``: the store answers
    again. Returns that state (``dead`` — the record's id once armed)."""
    server = fake._fake_server
    real_commit, real_get = server.commit, server.batch_get_documents
    state = {"armed": True, "dead": None}

    def _commit(request, metadata=None, **kwargs):
        if state["armed"]:
            for write in request.get("writes") or []:
                record_id = _record_id(server, write)
                if record_id and record_id not in skip_ids:
                    state["armed"], state["dead"] = False, record_id
                    if retry_answers_exists:
                        real_commit(request, metadata=metadata, **kwargs)
                        raise AlreadyExists("409 Document already exists")
                    if land:
                        real_commit(request, metadata=metadata, **kwargs)
                    raise RuntimeError("connection reset")
        return real_commit(request, metadata=metadata, **kwargs)

    def _get(request, metadata=None, **kwargs):
        dead = state["dead"]
        if dead and any(server.rel(str(name)) == f"documents/{dead}"
                        for name in request.get("documents") or []):
            raise RuntimeError("firestore unreadable")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", _commit)
    monkeypatch.setattr(server, "batch_get_documents", _get)
    return state


def unreadable_after_commit(fake, monkeypatch, record_id: str) -> dict:
    """``documents/{record_id}`` commits normally; from that commit on,
    every read of it fails (until ``state["dead"] = None``) — the store
    going dark right after ANOTHER call filed the record."""
    server = fake._fake_server
    real_commit, real_get = server.commit, server.batch_get_documents
    state = {"dead": None}

    def _commit(request, metadata=None, **kwargs):
        response = real_commit(request, metadata=metadata, **kwargs)
        if any(_record_id(server, w) == record_id
               for w in request.get("writes") or []):
            state["dead"] = record_id
        return response

    def _get(request, metadata=None, **kwargs):
        dead = state["dead"]
        if dead and any(server.rel(str(name)) == f"documents/{dead}"
                        for name in request.get("documents") or []):
            raise RuntimeError("firestore unreadable")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "commit", _commit)
    monkeypatch.setattr(server, "batch_get_documents", _get)
    return state


def unreadable_documents(fake, monkeypatch, *paths: str) -> dict:
    """Every read naming one of *paths* (``"dossiers/d1"``…) fails, until
    the test empties ``state["paths"]`` (the store answers again).
    ``state["refused"]`` lists the paths whose read was refused, in order."""
    server = fake._fake_server
    real_get = server.batch_get_documents
    state = {"paths": set(paths), "refused": []}

    def _get(request, metadata=None, **kwargs):
        for name in request.get("documents") or []:
            rel = server.rel(str(name))
            if rel in state["paths"]:
                state["refused"].append(rel)
                raise RuntimeError("firestore unreadable")
        return real_get(request, metadata=metadata, **kwargs)

    monkeypatch.setattr(server, "batch_get_documents", _get)
    return state
