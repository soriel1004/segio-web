import sys
import json
import subprocess
import shutil
import time
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import urlencode
from urllib.request import urlopen

from segio_archive import native_archive


class NativeRegistrationTests(unittest.TestCase):
    def exercise(self,frozen):
        opened=[];written=[]
        class Key:
            def __init__(self,path):self.path=path
            def __enter__(self):return self.path
            def __exit__(self,*args):pass
        def create(root,path):
            opened.append((root,path))
            return Key(path)
        registry=SimpleNamespace(HKEY_CURRENT_USER='USER',REG_SZ=1,CreateKey=create,
            SetValueEx=lambda *args:written.append(args))
        runtime=SimpleNamespace(executable=r'C:\Program Files\SEGIO\SEGIOArchive.exe' if frozen else '/tmp/SEGIO Program/python.exe',frozen=frozen)
        with patch.dict(sys.modules,{'winreg':registry}),patch.object(native_archive,'os',SimpleNamespace(name='nt')),patch.object(native_archive,'sys',runtime):
            self.assertTrue(native_archive.register())
        self.assertTrue(all(root=='USER' for root,path in opened))
        self.assertTrue(all(path.startswith('Software\\Classes\\segio-backup') for root,path in opened))
        command=next(args[-1] for args in written if args[0].endswith('command'))
        self.assertIn('manage-url "%1"',command)
        self.assertTrue(command.startswith('"'))
        return command

    def test_frozen_program_registers_only_a_quoted_current_user_handler(self):
        self.assertIn('SEGIOArchive.exe',self.exercise(True))

    def test_source_program_registers_the_python_launcher_with_absolute_paths(self):
        self.assertIn('native_archive.py',self.exercise(False))


class NativeStartupTests(unittest.TestCase):
    def setUp(self):
        from test_date_incremental import DateIncrementalTests
        self.fixture=DateIncrementalTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.old

    def uri(self,token):
        return 'segio-backup://open?'+urlencode({'archive':(self.root/'index.html').as_uri(),'token':token,'port':0})

    def signal(self,path):
        value=path.read_text(encoding='utf-8')
        return json.loads(value.removeprefix('window.segioManageReady(').removesuffix(');'))

    def test_real_source_entry_starts_from_unrelated_directory_with_spaces_and_quiet_python(self):
        location=self.fixture.root/'프로그램 폴더'/'segio_archive'
        shutil.copytree(Path(native_archive.__file__).parent,location,ignore=shutil.ignore_patterns('__pycache__'))
        for quiet in (False,True):
            with self.subTest(quiet=quiet):
                token=('b' if quiet else 'a')*64
                entry=location/'native_archive.py'
                args=[sys.executable,str(entry),'manage-url',self.uri(token)]
                if quiet:
                    code="import runpy,sys;sys.stdout=None;sys.stderr=None;sys.argv=sys.argv[1:];runpy.run_path(sys.argv[0],run_name='__main__')"
                    args=[sys.executable,'-c',code,*args[1:]]
                child=subprocess.Popen(args,cwd=self.fixture.root,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
                try:
                    path=native_archive.signal_path(self.root,token)
                    deadline=time.monotonic()+15
                    while not path.exists() and time.monotonic()<deadline and child.poll() is None:time.sleep(.02)
                    self.assertIsNone(child.poll())
                    data=self.signal(path)
                    self.assertEqual(set(data),{'protocol','token','state','port'})
                    self.assertEqual(data['state'],'ready')
                    with urlopen('http://127.0.0.1:'+str(data['port'])+'/'+token+'/bridge/connect',timeout=3) as response:
                        self.assertEqual(response.status,200)
                    self.assertNotIn('port=0',path.read_text())
                finally:
                    child.terminate();child.communicate(timeout=10)

    def test_startup_failure_publishes_a_finite_error_without_exception_or_credential_data(self):
        token='c'*64
        with patch('segio_archive.viewer.Viewer',side_effect=PermissionError('PRIVATE_BODY_OR_PATH')) as viewer,patch('segio_archive.preferences.load_output_folder',return_value=str(self.fixture.root/'output')):
            with self.assertRaisesRegex(ValueError,'PC 삭제 관리 프로그램'):native_archive.run_url(self.uri(token))
        self.assertEqual(viewer.call_count,1)
        data=self.signal(native_archive.signal_path(self.root,token))
        self.assertEqual(data['state'],'failed')
        self.assertNotIn('PRIVATE_BODY_OR_PATH',json.dumps(data))
        error=json.loads((self.fixture.root/'output'/'last-client-failure.json').read_bytes())
        self.assertEqual(error['phase'],'PC 삭제 관리 연결')
        self.assertNotIn('PRIVATE_BODY_OR_PATH',json.dumps(error))

    def test_readiness_rejects_symlink_directory_and_invalid_nonce(self):
        with self.assertRaises(ValueError):native_archive.signal_path(self.root,'invalid')
        if sys.platform=='win32':return  # Creating symlinks can require Windows developer mode.
        search=self.root/'search'
        search.rename(self.root/'search-original')
        search.symlink_to(self.root/'search-original',target_is_directory=True)
        with self.assertRaises(ValueError):native_archive.signal_path(self.root,'a'*64)
