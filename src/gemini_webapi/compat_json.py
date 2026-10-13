import sys

try:
    import orjson as json
except ImportError:
    import json as _json
    class _JsonWrapper:
        JSONDecodeError = _json.JSONDecodeError
        def loads(self, s, *args, **kwargs):
            if isinstance(s, (bytes, bytearray)):
                s = s.decode("utf-8")
            return _json.loads(s, *args, **kwargs)
        def dumps(self, obj, *args, **kwargs):
            res = _json.dumps(obj, *args, **kwargs)
            return res.encode("utf-8")
        def dump(self, obj, fp, *args, **kwargs):
            return _json.dump(obj, fp, *args, **kwargs)
        def load(self, fp, *args, **kwargs):
            return _json.load(fp, *args, **kwargs)
    json = _JsonWrapper()
