import os
import tempfile
import unittest
from unittest.mock import patch

_data = tempfile.TemporaryDirectory()
os.environ['MYKMAN_DATA_DIR'] = _data.name

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from fastapi import HTTPException
from fastapi.testclient import TestClient
from app.models import Base, Artist, Album, Song, Comparison, ComparisonQueueItem
from app import main
from app.queue_selection import select_queue_pairs
from app.pair_selector import _best_pair, _RECENT_SONG_IDS, _RECENT_PAIR_KEYS
from app.scoring import classify_release_type, album_scores, artist_scores
from app.glicko import update_pair


class RankingTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine, autoflush=False)
        artist = Artist(name='Test')
        album = Album(title='Reputation', artist=artist, total_track_count=10)
        self.db.add_all([Song(title=f'Song {i}', album=album) for i in range(20)])
        self.db.commit()
        _RECENT_SONG_IDS.clear()
        _RECENT_PAIR_KEYS.clear()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def vote(self, a, b, **kwargs):
        with patch.object(main, 'require_admin'), patch.object(main, 'append_event'):
            return main.submit_comparison(main.CompareBody(song_a_id=a, song_b_id=b,
                                         winner_id=kwargs.pop('winner_id', a), **kwargs), None, self.db)

    def test_xml_songs_fill_without_apple_ids_and_keep_queue(self):
        items = main._ensure_comparison_queue(self.db, 8)
        self.assertEqual(len(items), 8)
        self.assertEqual(len({sid for i in items for sid in (i.song_a_id, i.song_b_id)}), 16)
        ids = [i.id for i in items]
        self.assertEqual([i.id for i in main._ensure_comparison_queue(self.db, 8)], ids)

    def test_full_history_excluded_and_exhaustion(self):
        self.db.query(Song).filter(Song.id > 2).delete()
        self.db.add(Comparison(song_a_id=2, song_b_id=1, winner_id=1))
        self.db.commit()
        self.assertEqual(select_queue_pairs(self.db, 4), [])

    def test_small_library_partial_queue(self):
        self.db.query(Song).filter(Song.id > 3).delete()
        self.db.commit()
        self.assertEqual(len(main._ensure_comparison_queue(self.db, 8)), 1)

    def test_stale_queue_replaced(self):
        self.db.add(ComparisonQueueItem(song_a_id=999, song_b_id=998))
        self.db.commit()
        self.assertEqual(len(main._ensure_comparison_queue(self.db, 4)), 4)
        self.assertEqual(self.db.query(ComparisonQueueItem).filter_by(status='stale').count(), 1)

    def test_skip_changes_no_ratings_or_history(self):
        self.vote(1, 2, winner_id=None, skip=True)
        self.assertEqual(self.db.query(Comparison).count(), 0)
        for sid in (1, 2):
            song = self.db.get(Song, sid)
            self.assertEqual((song.glicko_rating, song.glicko_rd, song.comparison_count), (1500, 350, 0))

    def test_tie_is_evidence(self):
        self.vote(1, 2, winner_id=None)
        self.assertEqual(self.db.query(Comparison).count(), 1)
        self.assertLess(self.db.get(Song, 1).glicko_rd, 350)

    def test_retry_does_not_double_count(self):
        self.vote(1, 2)
        self.db.rollback()
        with self.assertRaises(HTTPException) as error:
            self.vote(2, 1, winner_id=1)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.db.query(Comparison).count(), 1)

    def test_self_comparison_rejected(self):
        with self.assertRaises(HTTPException):
            self.vote(1, 1)
        self.assertEqual(self.db.query(Comparison).count(), 0)

    def test_placement_uses_pre_vote_ratings(self):
        result = self.vote(1, 2, queue_size=8)
        self.assertEqual(self.db.get(Song, 1).placement_lo, 1500)
        self.assertEqual(self.db.get(Song, 2).placement_hi, 1500)
        self.assertEqual(len(result['queue']['pairs']), 8)

    def test_selector_queries_do_not_grow_per_pair(self):
        statements = []
        def capture(conn, cursor, sql, params, context, many):
            statements.append(sql)
        event.listen(self.engine, 'before_cursor_execute', capture)
        self.assertEqual(len(select_queue_pairs(self.db, 8)), 8)
        self.assertEqual(len(statements), 3)

    def test_exclusions_never_relaxed(self):
        songs = self.db.query(Song).filter(Song.id <= 2).all()
        self.assertIsNone(_best_pair(songs, {(1, 2)}))
        self.assertIsNone(_best_pair([songs[0], songs[0]]))

    def test_ep_is_a_word_not_substring(self):
        for title in ('Reputation', 'Depeche Mode', 'Keep Going'):
            self.assertEqual(classify_release_type(Album(title=title, total_track_count=10)), 'album')
        self.assertEqual(classify_release_type(Album(title='Test - EP', total_track_count=5)), 'ep')

    def test_glicko_symmetry_and_winner_direction(self):
        a, b = update_pair(1500, 350, .06, 1500, 350, .06, 1)
        self.assertGreater(a[0], 1500)
        self.assertAlmostEqual(a[0] + b[0], 3000)
        self.assertAlmostEqual(a[1], b[1])

    def test_score_pages_smoke(self):
        self.assertEqual(len(album_scores(self.db)), 1)
        self.assertEqual(len(artist_scores(self.db)), 1)

    def test_http_queue_vote_and_template(self):
        def session():
            with Session(self.engine, autoflush=False) as db:
                yield db
        main.app.dependency_overrides[main.get_session] = session
        try:
            with patch.object(main, 'is_admin', return_value=True), patch.object(main, 'require_admin'), patch.object(main, 'append_event'):
                client = TestClient(main.app)
                self.assertEqual(client.get('/compare').status_code, 200)
                response = client.get('/api/next-pairs?n=8')
                self.assertEqual(response.status_code, 200)
                pair = response.json()['pairs'][0]
                payload = dict(song_a_id=pair['a']['id'], song_b_id=pair['b']['id'], winner_id=pair['a']['id'], queue_size=8)
                self.assertEqual(client.post('/api/compare', json=payload).status_code, 200)
                self.assertEqual(client.post('/api/compare', json=payload).status_code, 409)
                for name in main.templates.env.list_templates():
                    main.templates.env.get_template(name)
        finally:
            main.app.dependency_overrides.clear()

    def test_queue_payload_uses_one_query(self):
        items = main._ensure_comparison_queue(self.db, 8)
        queries = []
        event.listen(self.engine, 'before_cursor_execute', lambda *args: queries.append(args[2]))
        self.assertEqual(len(main._comparison_queue_payload(items, self.db)['pairs']), 8)
        self.assertEqual(len(queries), 1)

    def test_refill_reserves_writer_after_read(self):
        self.db.query(Comparison).all()
        queries = []
        event.listen(self.engine, 'before_cursor_execute', lambda *args: queries.append(args[2]))
        main._ensure_comparison_queue(self.db, 4)
        self.assertEqual(queries[0], 'BEGIN IMMEDIATE')

    def test_startup_does_not_run_metadata_repairs(self):
        with patch.dict(os.environ, {'MYKMAN_RUN_STARTUP_REPAIRS': '0'}), \
                patch.object(main, 'init_db') as initialize, \
                patch('app.db.SessionLocal') as session:
            main.on_startup()
        initialize.assert_called_once_with(main.engine)
        session.assert_not_called()

    def test_concurrent_retries_preserve_one_vote(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        engine = create_engine(f'sqlite:///{_data.name}/concurrent.db',
                               connect_args={'check_same_thread': False, 'timeout': 10})
        Base.metadata.create_all(engine)
        with Session(engine) as db:
            album = Album(title='Concurrency', artist=Artist(name='Concurrency'))
            db.add_all([Song(title=f'C {i}', album=album) for i in range(20)])
            db.commit()
        barrier = Barrier(2)
        def submit(reverse):
            with Session(engine, autoflush=False) as db:
                barrier.wait()
                try:
                    main.submit_comparison(main.CompareBody(
                        song_a_id=2 if reverse else 1, song_b_id=1 if reverse else 2,
                        winner_id=1), None, db)
                    return 200
                except HTTPException as error:
                    return error.status_code
        try:
            with patch.object(main, 'require_admin'), patch.object(main, 'append_event'):
                with ThreadPoolExecutor(max_workers=2) as pool:
                    self.assertEqual(sorted(pool.map(submit, [False, True])), [200, 409])
            with Session(engine) as db:
                self.assertEqual(db.query(Comparison).count(), 1)
                self.assertEqual(db.get(Song, 1).comparison_count, 1)
                self.assertEqual(db.get(Song, 2).comparison_count, 1)
        finally:
            engine.dispose()


if __name__ == '__main__':
    unittest.main()
