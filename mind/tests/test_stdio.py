import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from leeroy_mind.lifecycle import spawn_mind, CREATE_NO_WINDOW
from leeroy_mind.stdio import rotate_before_launch


def test_launch_rotation_keeps_fourteen_backups_and_same_day_output(tmp_path,monkeypatch):
    path=tmp_path/'mind-stdout.log'
    path.write_text('before\n')
    prior=int(datetime(2026,10,16,12).timestamp())
    os.utime(path,(prior,prior))
    for day in range(1,16): (tmp_path/f'mind-stdout.log.2026-10-{day:02}').write_text('older\n')
    monkeypatch.setattr('logging.handlers.time.time',lambda:int(datetime(2026,10,17,12).timestamp()))
    rotate_before_launch(path)
    assert (tmp_path/'mind-stdout.log.2026-10-16').read_text()=='before\n'
    assert len(list(tmp_path.glob('mind-stdout.log.*')))==14
    path.write_text('after\n')
    today=int(datetime(2026,10,17,12).timestamp()); os.utime(path,(today,today))
    rotate_before_launch(path)
    assert path.read_text()=='after\n'


def test_launcher_passes_an_append_handle_for_all_child_output(tmp_path):
    calls=[]
    def popen(*args,**options):
        assert options['stdout'].mode=='ab' and not options['stdout'].closed
        assert options['stderr']==subprocess.STDOUT
        assert 'LEEROY_CAPTURE_STDIO' not in options['env']
        options['stdout'].write(b'launcher diagnostic\n')
        calls.append(args)
        return SimpleNamespace(pid=101)
    spawn_mind(tmp_path,tmp_path/'data',Path(sys.executable),popen=popen)
    assert calls[0][0][1:4]==['-m','leeroy_mind','run']
    assert (tmp_path/'data/logs/mind-stdout.log').read_text()=='launcher diagnostic\n'


@pytest.mark.parametrize('source,expected',[
    ('raise ModuleNotFoundError("synthetic_missing_dependency")','ModuleNotFoundError: synthetic_missing_dependency'),
    ('this is invalid syntax !','SyntaxError'),
    ('import os\nos.write(2,b"synthetic OS stderr\\n")\nraise SystemExit(2)','synthetic OS stderr'),
])
def test_child_failing_before_main_or_below_python_stderr_is_logged(tmp_path,source,expected):
    package=tmp_path/'mind/stdio_failure_fixture'; package.mkdir(parents=True)
    (package/'__init__.py').write_text(source)
    data=tmp_path/'data'; logs=data/'logs'; logs.mkdir(parents=True)
    (logs/'mind-stdout.log').write_text('earlier output\n')
    def portable_popen(args,**options):
        options['creationflags']=CREATE_NO_WINDOW if os.name=='nt' else 0
        # Exercise the launcher's actual inherited handles with a disposable module.
        # Never launch a service-shaped command or relax the suite's live guard.
        return subprocess.Popen([args[0],'-m','stdio_failure_fixture'],**options)
    child=spawn_mind(tmp_path,data,Path(sys.executable),popen=portable_popen)
    assert child.wait(timeout=10)!=0
    text=(logs/'mind-stdout.log').read_text()
    assert text.startswith('earlier output\n') and expected in text
