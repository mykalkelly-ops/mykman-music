import unittest
from datetime import datetime, timedelta

from starlette.requests import Request
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, ListeningProgress, ListeningTrack, Comparison, Song, Artist, Album
from app.listening import (InventoryBody, ProgressBody, save_inventory, inventory_view,
    update_progress, target_key, import_inventory, change_progress, export_progress, listening_page)


class ListeningTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite://', connect_args={'check_same_thread': False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.db = Session(self.engine)
        self.body = InventoryBody(tracks=[
            {'library_id':'one', 'catalog_id':'101', 'artist':'A', 'album':'B', 'title':'First', 'track':1},
            {'library_id':'two', 'artist':'A', 'album':'B', 'title':'Second', 'track':2},
        ], recent_catalog_ids=['101'])
        save_inventory(self.db, self.body)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def tracks(self):
        return inventory_view(self.db)['artists'][0]['albums'][0]['tracks']

    def test_recent_playback_and_library_membership_do_not_imply_completion(self):
        tracks = self.tracks()
        self.assertTrue(tracks[0]['recent'])
        self.assertEqual([t['status'] for t in tracks], ['unknown', 'unknown'])

    def test_album_finish_song_correction_and_rescan(self):
        update_progress(self.db, ProgressBody(key=target_key('album','A','B'), status='complete'))
        self.assertEqual([t['status'] for t in self.tracks()], ['complete', 'complete'])
        update_progress(self.db, ProgressBody(key=target_key('song','two'), status='unheard'))
        save_inventory(self.db, self.body)
        self.assertEqual([t['status'] for t in self.tracks()], ['complete', 'unheard'])
        update_progress(self.db, ProgressBody(key=target_key('song','two'), status='unknown'))
        self.assertEqual(self.tracks()[1]['status'], 'unknown')

    def test_queuing_does_not_reset_completion_and_finish_clears_descendants(self):
        album = target_key('album','A','B')
        song = target_key('song','one')
        update_progress(self.db, ProgressBody(key=album, status='complete'))
        update_progress(self.db, ProgressBody(key=song, queue='top'))
        self.assertEqual(self.tracks()[0]['status'],'complete')
        update_progress(self.db, ProgressBody(key=album, status='complete'))
        self.assertEqual(inventory_view(self.db)['queue'], [])

    def test_missing_and_new_tracks_do_not_erase_or_inherit_old_progress(self):
        update_progress(self.db, ProgressBody(key=target_key('artist','A'), status='complete'))
        save_inventory(self.db, InventoryBody(tracks=[
            {'library_id':'new', 'artist':'A', 'album':'C', 'title':'New release'}]))
        tracks = [t for a in inventory_view(self.db)['artists'][0]['albums'] for t in a['tracks']]
        self.assertEqual(len(tracks),3)
        self.assertEqual(next(t for t in tracks if t['id']=='new')['status'], 'unknown')

    def test_queue_survives_session_restart(self):
        update_progress(self.db, ProgressBody(key=target_key('song','two'), status='in_progress'))
        self.db.close()
        self.db = Session(self.engine)
        self.assertEqual(inventory_view(self.db)['queue'][0]['key'], target_key('song','two'))

    def test_rescan_never_changes_ranking_data(self):
        artist = Artist(name='Ranking Artist')
        album = Album(artist=artist, title='Ranking Album')
        a = Song(album=album, title='a', glicko_rating=1700)
        b = Song(album=album, title='b', glicko_rating=1300)
        self.db.add_all([a,b]); self.db.flush()
        comparison = Comparison(song_a_id=a.id, song_b_id=b.id, winner_id=a.id)
        self.db.add(comparison); self.db.commit()
        save_inventory(self.db, self.body)
        update_progress(self.db, ProgressBody(key=target_key('artist','A'), status='complete'))
        self.assertEqual(self.db.query(Comparison).count(),1)
        self.assertEqual(self.db.get(Song,a.id).glicko_rating,1700)
        self.assertEqual(self.db.query(Song).count(),2)

    def test_invalid_target_and_unauthorized_mutations_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            update_progress(self.db, ProgressBody(key='["song","missing"]',status='complete'))
        request = Request({'type':'http','headers':[]})
        calls = [lambda: import_inventory(self.body, request, self.db),
            lambda: change_progress(ProgressBody(key=target_key('song','one'),status='complete'),request,self.db),
            lambda: export_progress(request,self.db)]
        for call in calls:
            with self.assertRaises(HTTPException) as error:
                call()
            self.assertEqual(error.exception.status_code,403)

    def test_authorized_page_and_export_render(self):
        from unittest.mock import patch
        request = Request({'type':'http','headers':[], 'state':{'is_admin':True}})
        with patch('app.listening.is_admin',return_value=True):
            page = listening_page(request, q='A', db=self.db)
        self.assertIn(b'First',page.body)
        with patch('app.listening.require_admin'):
            export = export_progress(request,self.db)
        import json
        self.assertEqual(len(json.loads(export.body)['tracks']),2)

    def test_album_queue_advances_after_song_completion(self):
        update_progress(self.db, ProgressBody(key=target_key('album','A','B'),queue='top'))
        self.assertEqual(inventory_view(self.db)['next_track']['id'],'one')
        update_progress(self.db, ProgressBody(key=target_key('song','one'),status='complete'))
        self.assertEqual(inventory_view(self.db)['next_track']['id'],'two')

    def test_starting_artist_does_not_start_every_song(self):
        update_progress(self.db, ProgressBody(key=target_key('artist','A'),status='in_progress'))
        self.assertEqual([t['status'] for t in self.tracks()],['unknown','unknown'])
        self.assertEqual(inventory_view(self.db)['queue'][0]['status'],'in_progress')


if __name__ == '__main__':
    unittest.main()
