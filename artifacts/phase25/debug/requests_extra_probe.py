import tracemalloc
from requests import Response

class Raw:
    def __init__(self, parts):
        self.parts = iter(parts)
        self.count = 0
    def stream(self, *args, **kwargs):
        for chunk in self.parts:
            self.count += 1
            yield chunk

def response(parts):
    r = Response()
    r.encoding = None
    r.raw = Raw(parts)
    return r

text = '中文多字节流与UTF8检测' * 100
encoded = text.encode('utf8')
r = response(encoded[i:i+1] for i in range(len(encoded)))
it = r.iter_content(1, decode_unicode=True)
assert r.raw.count == 0
assert ''.join(it) == text
r = response([b'a' * 65536, b'\xe4', b'\xb8', b'\xad'])
assert ''.join(r.iter_content(1024, decode_unicode=True)) == 'a' * 65536 + '中'
# Warm detector imports before measuring only the first yield of a lazy source.
assert ''.join(response([b'warm']).iter_content(1024, decode_unicode=True)) == 'warm'
r = response(b'z' * 1024 for _ in range(100000))
it = r.iter_content(1024, decode_unicode=True)
assert r.raw.count == 0
tracemalloc.start()
chunk = next(it)
current, peak = tracemalloc.get_traced_memory()
tracemalloc.stop()
assert chunk == 'z' * 1024
assert r.raw.count == 64, r.raw.count
assert peak < 1024 * 1024, peak
assert r._content is False and not r._content_consumed
print('PASS: unknown-encoding split UTF8; late UTF8 after ASCII sample; lazy 100MiB source first yield after 64KiB; first-yield traced peak', peak)
