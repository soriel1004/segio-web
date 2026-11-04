import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

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
