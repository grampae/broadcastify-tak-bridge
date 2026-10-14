import time

class DedupCache:
    def __init__(self):
        self._cache = {}

    def seen(self, uid, ttl_seconds=300):
        now = time.time()
        expired = [k for k, v in self._cache.items() if v < now]
        for k in expired:
            del self._cache[k]
        if uid in self._cache:
            return True
        self._cache[uid] = now + ttl_seconds
        return False

    def invalidate(self, uid):
        self._cache.pop(uid, None)

    def size(self):
        return len(self._cache)
