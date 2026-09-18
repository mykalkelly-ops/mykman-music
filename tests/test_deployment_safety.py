import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

spec = importlib.util.spec_from_file_location('deployment_safety', Path(__file__).resolve().parents[1] / 'scripts/deployment_safety.py')
safety = importlib.util.module_from_spec(spec)
spec.loader.exec_module(safety)


class DeploymentSafetyTests(unittest.TestCase):
    def test_wal_backup_preserves_rows_and_detects_rewrites(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'source.db'
            backup = Path(directory) / 'backup.db'
            db = sqlite3.connect(source)
            try:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE comparisons (id INTEGER PRIMARY KEY, winner_id INTEGER)')
                db.execute('INSERT INTO comparisons VALUES (1, 10)')
                db.commit()
                self.assertEqual(safety.backup(source, backup)['comparison_count'], 1)
                db.execute('INSERT INTO comparisons VALUES (2, 20)')
                db.commit()
                self.assertEqual(safety.verify(source, backup)['additional_comparisons'], 1)
                db.execute('UPDATE comparisons SET winner_id=99 WHERE id=1')
                db.commit()
                with self.assertRaisesRegex(RuntimeError, 'missing or changed'):
                    safety.verify(source, backup)
                with self.assertRaises(FileExistsError):
                    safety.backup(source, source)
            finally:
                db.close()

    def test_missing_source_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / 'missing.db'
            with self.assertRaises(FileNotFoundError):
                safety.open_readonly(source)
            self.assertFalse(source.exists())
