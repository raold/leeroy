"""Plan and apply evidence-only cycle repairs; no memory text leaves this module."""
import json
from dataclasses import dataclass

from leeroy_mind.storage.provenance import ProvenanceError, canonical, edges, reaches


@dataclass(frozen=True)
class Counts:
    self_refs: int
    cycle_edges: int


@dataclass(frozen=True)
class Repair:
    changed: dict[int, tuple[str, ...]]
    before: Counts
    after: Counts
    self_dropped: int
    cycle_dropped: int


class Graph:
    def __init__(self, conn):
        self.conn = conn
        self.evidence = {}
        self.cache = {}
        for row in conn.execute("SELECT id,evidence FROM memory ORDER BY id"):
            refs = json.loads(row["evidence"])
            if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
                raise ProvenanceError("Invalid memory evidence")
            self.evidence[row["id"]] = tuple(refs)

    def children(self, ref):
        ref = canonical(ref)
        if ref.startswith("memory:") and ref[7:].isdigit():
            return edges(self.conn, ref, memory_evidence=self.evidence).children
        if ref not in self.cache:
            self.cache[ref] = edges(self.conn, ref).children
        return self.cache[ref]

    def counts(self):
        """Count self references and DFS back edges without Python recursion.

        Back edges describe the graph, not the number of references to remove:
        shared cycles can require several memory references to be dropped.
        """
        selfs = sum(canonical(r) == f"memory:{i}"
                    for i, refs in self.evidence.items() for r in refs)
        color, back = {}, 0

        def children(ref):
            return (canonical(r) for r in self.children(ref)
                    if not (ref.startswith("memory:") and canonical(r) == ref))

        for ident in self.evidence:
            root = f"memory:{ident}"
            if root in color:
                continue
            color[root] = 1
            stack = [(root, iter(children(root)))]
            while stack:
                current, pending = stack[-1]
                for child in pending:
                    if color.get(child) == 1:
                        back += 1
                    elif child not in color:
                        color[child] = 1
                        stack.append((child, iter(children(child))))
                        break
                else:
                    color[current] = 2
                    stack.pop()
        return Counts(selfs, back)


def plan(conn):
    """Remove only references that participate in a cycle at the time of removal.

    Ascending memory ids make the repair deterministic. A retained edge that no
    longer closes a cycle is left intact. Bound exhaustion aborts the entire plan.
    """
    graph = Graph(conn)
    before = graph.counts()
    changed, selfs, cycles = {}, 0, 0
    for ident, refs in graph.evidence.items():
        target = f"memory:{ident}"
        keep = []
        for ref in refs:
            if canonical(ref) == target:
                selfs += 1
            elif reaches(graph.children, ref, target):
                cycles += 1
            else:
                keep.append(ref)
        if tuple(keep) != refs:
            graph.evidence[ident] = changed[ident] = tuple(keep)
    after = graph.counts()
    if after != Counts(0, 0):
        raise ProvenanceError("Cycle verification failed")
    return Repair(changed, before, after, selfs, cycles)


def apply(conn):
    """Repair and verify in the caller's write transaction; never commit here."""
    result = plan(conn)
    conn.executemany("UPDATE memory SET evidence=? WHERE id=?",
                     [(json.dumps(refs), ident) for ident, refs in result.changed.items()])
    if Graph(conn).counts() != Counts(0, 0):
        raise ProvenanceError("Cycle verification failed")
    return result
