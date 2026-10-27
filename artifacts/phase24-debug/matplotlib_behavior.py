import matplotlib
from packaging.version import Version
cases=[('3.4.2',(3,4,2),None,None,None),('3.4.2rc1',(3,4,2),('rc',1),None,None),('3.4.2.dev3',(3,4,2),None,3,None),('3.4.2.post4',(3,4,2),None,None,4),('3.4.2.post4+gabc',(3,4,2),None,None,4)]
original=matplotlib.__version__
for raw,release,pre,dev,post in cases:
    matplotlib.__version__=raw; result=matplotlib.version_info
    assert isinstance(result,Version) and (result.release,result.pre,result.dev,result.post)==(release,pre,dev,post), (raw,result)
    print(raw, result.release,result.pre,result.dev,result.post)
matplotlib.__version__=original
assert Version('3.4.2.dev1') < Version('3.4.2rc1') < Version('3.4.2') < Version('3.4.2.post1')
assert matplotlib.version_info == Version(original)
print('original version preserved:',original)
