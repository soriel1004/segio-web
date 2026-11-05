from contextlib import closing
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import patch,Mock

import test_date_incremental as fixtures
from segio_archive.archive import verify_archive
from segio_archive.compact_mail import digest_file,search
from segio_archive.date_incremental import DateBaseline
from segio_archive.delete_transaction import journal_path,recover
from segio_archive.local_delete import DeletePlan
from segio_archive.search_page import install_search_page
from segio_archive.viewer import Viewer


class FastDeleteTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixtures.DateIncrementalTests();self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root=self.fixture.old

    def plan(self,ids):
        plan=DeletePlan(self.root);self.addCleanup(plan.close)
        plan.add(ids);plan.seal()
        return plan

    def test_document_deletion_never_reads_or_copies_unselected_mail_or_attachments(self):
        old={p:(p.stat().st_ino,p.stat().st_mtime_ns,p.stat().st_size) for p in (self.root/'mail').rglob('*') if p.is_file()}
        plan=self.plan(['approval-1'])
        original=Path.open
        def guarded(path,*args,**kwargs):
            if path in old and path.name!='sources.json':raise AssertionError('Unselected mail payload was opened')
            return original(path,*args,**kwargs)
        with patch.object(Path,'open',guarded),patch('os.link',side_effect=AssertionError('No backup-wide cloning')),patch('segio_archive.portable_mail.build_portable_mail',side_effect=AssertionError('No whole mailbox audit')):
            plan.run()
        for path,before in old.items():self.assertEqual((path.stat().st_ino,path.stat().st_mtime_ns,path.stat().st_size),before)
        self.assertTrue(verify_archive(self.root)['ok'])

    def test_only_the_affected_static_search_chunk_is_replaced(self):
        manifest=json.loads((self.root/'manifest.json').read_bytes())
        catalog=json.loads((self.root/'catalog.json').read_bytes())
        original=catalog['items'][0]
        with closing(sqlite3.connect(self.root/'catalog.sqlite3')) as db:
            for number in range(1200):
                identity='bulk-'+str(number)
                catalog['items'].append(dict(original,id=identity,title=identity))
                db.execute('INSERT INTO items VALUES (?,?,?,?,?,?)',('approval-'+identity,'approval',identity,'결재 문서',identity,original['path']))
                db.execute('INSERT INTO document_records VALUES (?,?)',('approval-'+identity,'{}'))
            db.commit()
        (self.root/'catalog.json').write_text(json.dumps(catalog))
        manifest['counts']['approval']=1201
        for name in ('catalog.json','catalog.sqlite3'):manifest['files'][name]=digest_file(self.root/name)
        install_search_page(self.root,manifest)
        identity='approval-bulk-600'
        with closing(sqlite3.connect(self.root/'search/parts.sqlite3')) as db:
            affected=db.execute('SELECT chunk FROM parts WHERE id=?',(identity,)).fetchone()[0]
        untouched={p:(p.stat().st_ino,p.stat().st_mtime_ns,p.read_bytes()) for p in (self.root/'search').glob('part-*.js') if p!=self.root/affected}
        self.assertGreaterEqual(len(untouched),2)
        plan=self.plan([identity])
        original_open=Path.open
        def guarded(path,*args,**kwargs):
            if path in untouched:raise AssertionError('An unaffected search chunk was opened')
            return original_open(path,*args,**kwargs)
        with patch.object(Path,'open',guarded):plan.run()
        for path,before in untouched.items():self.assertEqual((path.stat().st_ino,path.stat().st_mtime_ns,path.read_bytes()),before)
        self.assertEqual(search(self.root,query='bulk-600')['total'],0)
        self.assertTrue(verify_archive(self.root)['ok'])

    def test_power_loss_before_the_manifest_commit_recovers_all_original_bytes(self):
        import os
        original=self.fixture.snapshot()
        plan=self.plan(['approval-1','mail-000000000'])
        replace=os.replace
        def crash(source,destination):
            if Path(source).name=='manifest.json' and Path(source).parent.name=='new':raise KeyboardInterrupt('Power loss')
            return replace(source,destination)
        with patch('segio_archive.file_ops.os.replace',side_effect=crash),self.assertRaises(KeyboardInterrupt):plan.run()
        self.assertTrue(journal_path(self.root).is_file())
        self.assertFalse((self.root/'manifest.json').exists())
        DateBaseline(self.root,'employee')
        self.assertEqual(self.fixture.snapshot(),original)
        self.assertFalse(journal_path(self.root).exists())

    def test_power_loss_after_manifest_install_keeps_the_completed_deletion(self):
        import os
        plan=self.plan(['approval-1'])
        replace=os.replace
        def crash(source,destination):
            result=replace(source,destination)
            if Path(source).name=='manifest.json' and Path(source).parent.name=='new':raise KeyboardInterrupt('Power loss')
            return result
        with patch('segio_archive.file_ops.os.replace',side_effect=crash),self.assertRaises(KeyboardInterrupt):plan.run()
        viewer=Viewer(self.root);self.addCleanup(viewer.close)
        self.assertFalse((self.root/'documents/1').exists())
        self.assertEqual(search(self.root,kind='approval')['total'],0)
        self.assertTrue(verify_archive(self.root)['ok'])
        self.assertFalse(journal_path(self.root).exists())

    def test_cleanup_failure_reports_retained_files_and_next_open_finishes_cleanup(self):
        plan=self.plan(['approval-1'])
        with patch('segio_archive.delete_transaction.shutil.rmtree',side_effect=PermissionError('locked cleanup')):
            result=plan.run()
        self.assertTrue(result['warnings'])
        self.assertEqual(search(self.root,kind='approval')['total'],0)
        self.assertTrue(journal_path(self.root).exists())
        DateBaseline(self.root,'employee')
        self.assertFalse(journal_path(self.root).exists())
        self.assertTrue(verify_archive(self.root)['ok'])

    def test_final_journal_write_failure_reports_commit_as_done_and_can_recover(self):
        from segio_archive.archive import atomic_write
        plan=self.plan(['approval-1'])
        def fail(path,payload):
            if path==journal_path(self.root) and json.loads(payload).get('committed') is True:raise OSError('disk full')
            return atomic_write(path,payload)
        with patch('segio_archive.delete_transaction.atomic_write',side_effect=fail):result=plan.run()
        self.assertTrue(result['warnings'])
        self.assertEqual(search(self.root,kind='approval')['total'],0)
        DateBaseline(self.root,'employee')
        self.assertTrue(verify_archive(self.root)['ok'])

    def test_warning_report_and_counters_remove_only_selected_warning_mail(self):
        data=b''.join(b'From sender@example.test Tue Oct 20 13:00:00 2026\nSubject: =?utf-8?b?A?=\n\nBody\n\n' for _ in range(2))
        (self.fixture.mail/'inbox.mbox').write_bytes(data)
        self.root=self.fixture.old=self.fixture.export(2)
        from segio_archive.mail_import import build_local_catalog
        build_local_catalog(self.root,'employee')
        matches=search(self.root,query='=?utf-8?b?A?=',kind='mail')['items']
        self.assertEqual(len(matches),2)
        self.plan([matches[0]['id']]).run()
        manifest=json.loads((self.root/'manifest.json').read_bytes())
        self.assertEqual(manifest['portable_mail']['warning_count'],1)
        report=(self.root/'mail/processing-issues.html').read_text()
        self.assertNotIn(matches[0]['id'],report)
        self.assertIn(matches[1]['id'],report)
        self.plan([matches[1]['id']]).run()
        manifest=json.loads((self.root/'manifest.json').read_bytes())
        self.assertEqual(manifest['portable_mail']['warning_count'],0)
        self.assertFalse((self.root/'mail/processing-issues.html').exists())
        self.assertTrue(verify_archive(self.root)['ok'])

    def test_opening_a_completed_archive_does_not_start_an_all_mail_conversion(self):
        from segio_archive.gui import App
        messages=[]
        app=SimpleNamespace(running=False,select_archive=lambda:self.root,viewer=None,
            status=SimpleNamespace(set=messages.append),update_idletasks=lambda:None)
        with patch('segio_archive.gui.webbrowser.open'),patch('segio_archive.gui.threading',SimpleNamespace(Thread=Mock(side_effect=AssertionError('No audit on open')))):
            App.open_archive(app)
        self.assertIsInstance(app.viewer,Viewer)
        app.viewer.close()


if __name__=='__main__':unittest.main()
