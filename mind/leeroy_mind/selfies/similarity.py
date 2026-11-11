"""Bounded chosen-photo embeddings and uncalibrated similarity clusters."""
from itertools import combinations
import math
import sqlite3
import struct

MODEL = "ipadapter-vith-head-v1"
DIMENSIONS = 1024
# Uncalibrated cosine cutoff on the pinned encoder. Single-link components can
# chain: two endpoints need not be close. Preview clusters before choosing a top N.
PHOTO_CLUSTER_DISTANCE = .08
MAX_PHOTOS = 1024


def checked(vector):
    if (not isinstance(vector, (list, tuple)) or len(vector) != DIMENSIONS
            or any(type(v) not in (int,float) or not math.isfinite(v) for v in vector)):
        raise ValueError("embedding_invalid")
    norm = math.sqrt(math.sumprod(vector, vector))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("embedding_invalid")
    return tuple(v/norm for v in vector)


def cached(conn, guid, pick):
    try:
        row = conn.execute("SELECT vector FROM selfie_embedding WHERE guid=? AND pick=? AND model=?",
                           (guid,pick,MODEL)).fetchone()
        return checked(struct.unpack("<1024f", row[0])) if row else None
    except (sqlite3.OperationalError, ValueError, TypeError, struct.error):
        return None


def save(store, guid, pick, vector, now):
    value = struct.pack("<1024f", *checked(vector))
    with store.transaction():
        store.conn.execute("INSERT OR REPLACE INTO selfie_embedding VALUES(?,?,?,?,?)",
                           (guid,pick,MODEL,value,now))


def clusters(vectors):
    """Single-link components; every connecting edge meets the named cutoff."""
    if len(vectors) > MAX_PHOTOS:
        raise ValueError("too_many_photos")
    vectors = {g: checked(v) for g,v in vectors.items()}
    parent = {g:g for g in vectors}
    def root(g):
        while parent[g] != g:
            parent[g] = parent[parent[g]]
            g = parent[g]
        return g
    pairs = []
    for left,right in combinations(sorted(vectors),2):
        distance = max(0, min(2, 1-math.sumprod(vectors[left],vectors[right])))
        if distance <= PHOTO_CLUSTER_DISTANCE:
            pairs.append((distance,left,right))
            parent[root(right)] = root(left)
    groups = {}
    for guid in sorted(vectors):
        groups.setdefault(root(guid),[]).append(guid)
    return sorted((v for v in groups.values() if len(v)>1),key=lambda v:(-len(v),v)), sorted(pairs)


def choose(vectors, protected, top=30, *, release=()):
    groups,pairs = clusters(vectors)
    nearest = {}
    for score,a,b in pairs:
        nearest[a] = min(nearest.get(a,2),score)
        nearest[b] = min(nearest.get(b,2),score)
    selected,kept = [],[]
    for group in groups:
        # A protected member is already a permanent representative of this cluster.
        keeper = min((g for g in group if g in protected),
                     default=min((g for g in group if g not in release),default=min(group)))
        kept.append(keeper)
        selected.extend((len(group),nearest[g],g,keeper) for g in group if g != keeper and g not in protected)
    selected.sort(key=lambda item:(-item[0],item[1],item[2]))
    return selected[:top], kept, pairs
