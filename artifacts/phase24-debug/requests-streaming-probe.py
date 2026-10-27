from requests import Response
class Raw:
    def __init__(self): self.calls=0; self.data=b'hello'
    def stream(self, *args, **kwargs):
        self.calls += 1
        data,self.data=self.data,b''
        if data: yield data
r=Response(); r.raw=Raw(); r.encoding=None
chunks=r.iter_content(chunk_size=2, decode_unicode=True)
print('raw reads at iterator creation:',r.raw.calls)
print('chunks:',list(chunks))
