from requests import Response
class Raw:
    def __init__(self, chunks): self.calls=0; self.chunks=list(chunks)
    def stream(self, *args, **kwargs):
        self.calls += 1
        while self.chunks: yield self.chunks.pop(0)
def run(parts, encoding=None, decode=True):
    r=Response(); r.raw=Raw(parts); r.encoding=encoding
    chunks=r.iter_content(chunk_size=1, decode_unicode=decode)
    assert r.raw.calls == 0
    return list(chunks)
assert ''.join(run([b'hello'])) == 'hello'
assert ''.join(run([b'caf\xc3', b'\xa9'], encoding='utf-8')) == 'caf\u00e9'
assert b''.join(run([b'abc',b'def'],decode=False)) == b'abcdef'
assert run([],decode=True) == []
print('PASS: lazy read, body retention, split UTF-8, bytes mode, empty body')
