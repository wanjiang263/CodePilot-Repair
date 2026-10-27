import os, subprocess, tempfile
from pathlib import Path
python='/opt/miniconda3/envs/testbed/bin/python'
for enabled in (False, True):
    with tempfile.TemporaryDirectory() as t:
        root=Path(t); src=root/'src'; src.mkdir(); out=root/'out'
        (src/'index.rst').write_text('Example\n=======\n\nExample body.\n')
        (src/'conf.py').write_text("project='Example'\nmaster_doc='index'\nman_pages=[('index','one','one',[],1),('index','three','three',[],3)]\nman_make_section_directory=%s\n" % enabled)
        result=subprocess.run([python,'-m','sphinx','-b','man',str(src),str(out)],capture_output=True,text=True)
        actual=sorted(str(p.relative_to(out)) for p in out.rglob('*') if p.is_file() and not str(p.relative_to(out)).startswith('.doctrees'))
        expected=['man1/one.1','man3/three.3'] if enabled else ['one.1','three.3']
        print('enabled=',enabled,'exit=',result.returncode,'files=',actual,'expected=',expected,flush=True)
        if result.returncode or actual != expected:
            print(result.stderr[-2000:],flush=True); raise SystemExit(1)
