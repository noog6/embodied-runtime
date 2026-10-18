"""Durable image identities, isolation, retention and bounded lazy loading."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from embodied_runtime.attachments import ImageAttachment
from embodied_runtime.conversation_media import ConversationMediaStore, MediaRetentionConfiguration, MediaUnavailableError
from embodied_runtime.images import ImageLimits
from embodied_runtime.conversation_history import SQLiteConversationHistoryStore
from embodied_runtime.conversation_history.model import NewConversationTurn
from embodied_runtime.interaction import InteractionChannel
from tests.test_workspace_images import raster

NOW = datetime(2026, 10, 18, 15, tzinfo=UTC)


class ConversationMediaTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = MediaRetentionConfiguration(database_path=Path(self.temp.name) / 'media.sqlite3')
        self.now = NOW
        self.store = ConversationMediaStore(self.config, clock=lambda: self.now)
        self.images = (ImageAttachment('image/jpeg', raster(), source_message_id='SM1', received_at=NOW),
            ImageAttachment('image/png', raster('PNG', 'blue'), source_message_id='SM1',
                            attachment_index=1, received_at=NOW))

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def retain(self, images=None, **kwargs):
        return self.store.retain(images or self.images, channel='remote_text', message_identity='SM1',
                                 message_text='the plant photo', **kwargs)

    def test_bytes_metadata_identity_order_and_restart(self):
        records = self.retain()
        refs = [record['image_ref'] for record in records]
        self.assertEqual(len(set(refs)), 2)
        self.assertEqual(records[0]['integrity_hash'], hashlib.sha256(self.images[0].data).hexdigest())
        self.assertEqual(records[1]['attachment_index'], 1)
        self.assertIsNone(records[0]['captured_at'])
        self.assertEqual(records[0]['received_at'], NOW.isoformat())
        self.store.close()
        self.store = ConversationMediaStore(self.config, clock=lambda: self.now)
        loaded = self.store.load_many(refs[::-1])
        self.assertEqual([image.data for image in loaded], [image.data for image in self.images[::-1]])
        self.assertEqual(self.store.list_images()['images'], list(records))
        self.assertNotIn('data', self.store.list_images()['images'][0])
        self.assertEqual(self.config.database_path.stat().st_mode & 0o777, 0o600)

    def test_retry_is_idempotent_and_conflicting_bytes_rejected(self):
        records = self.retain()
        self.assertEqual(self.retain(), records)
        self.assertTrue(self.store.has_message('remote_text', 'SM1'))
        with self.assertRaisesRegex(ValueError, 'different content'):
            self.retain((replace(self.images[0], data=raster(color='green')),))
        self.assertEqual(len(self.store.list_images()['images']), 2)

    def test_failed_batch_rolls_back_and_capacity_does_not_evict(self):
        self.store.close()
        self.store = ConversationMediaStore(replace(self.config, max_items=1), clock=lambda: NOW)
        with self.assertRaisesRegex(ValueError, 'quota'):
            self.retain()
        self.assertFalse(self.store.has_message('remote_text', 'SM1'))
        self.assertEqual(self.store.list_images()['images'], [])
        first = self.retain(self.images[:1])
        with self.assertRaisesRegex(ValueError, 'quota'):
            self.retain(self.images[1:])
        self.assertEqual(self.store.load_many([first[0]['image_ref']])[0].data, self.images[0].data)

    def test_scope_isolation_for_list_load_delete_and_retry(self):
        ref = self.retain()[0]['image_ref']
        for scope in ({'operator_scope': 'someone_else'}, {'conversation_scope': 'another'}):
            other = ConversationMediaStore(replace(self.config, **scope), clock=lambda: NOW)
            try:
                self.assertEqual(other.list_images()['images'], [])
                self.assertFalse(other.has_message('remote_text', 'SM1'))
                with self.assertRaises(MediaUnavailableError):
                    other.load_many([ref])
                with self.assertRaises(MediaUnavailableError):
                    other.delete([ref])
            finally:
                other.close()

    def test_search_is_message_text_only_bounded_ambiguous_and_time_filtered(self):
        self.retain()
        results = self.store.list_images(query='plant', limit=1)
        self.assertTrue(results['has_more'])
        self.assertEqual(len(results['images']), 1)
        self.assertEqual(len(self.store.list_images(query='plant')['images']), 2)  # Caller must clarify identity.
        self.assertEqual(self.store.list_images(query='blue')['images'], [])
        self.assertEqual(self.store.list_images(after=(NOW+timedelta(seconds=1)).isoformat())['images'], [])
        self.assertEqual(self.store.list_images(before=NOW.isoformat())['images'], [])
        self.assertEqual(self.store.list_images(query="%' OR 1=1 --")['images'], [])
        for kwargs in ({'limit': 17}, {'offset': -1}, {'query': 'x'*201}, {'after': '2026-10-18'}):
            with self.assertRaises(ValueError):
                self.store.list_images(**kwargs)

    def test_expiry_is_enforced_before_cleanup_and_deletion_survives_restart(self):
        refs = [record['image_ref'] for record in self.retain()]
        self.store.delete([refs[0]])
        with self.assertRaisesRegex(MediaUnavailableError, 'deleted'):
            self.store.load_many([refs[0]])
        self.now += timedelta(days=7)
        self.assertEqual(self.store.list_images()['images'], [])
        with self.assertRaisesRegex(MediaUnavailableError, 'expired'):
            self.store.load_many([refs[1]])
        self.assertEqual(self.store.cleanup(limit=1), 0)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM pixels').fetchone()[0], 0)
        self.assertTrue(self.store.has_message('remote_text', 'SM1'))

    def test_missing_corrupt_and_legacy_images_honestly_unavailable(self):
        refs = [record['image_ref'] for record in self.retain()]
        with self.store._db:
            self.store._db.execute('DELETE FROM pixels WHERE id=?', (refs[0],))
            self.store._db.execute('UPDATE pixels SET data=? WHERE id=?', (b'broken', refs[1]))
        for ref in (*refs, 'MEDIA' + '0'*32, 'old-photo.jpg'):
            with self.assertRaises(MediaUnavailableError):
                self.store.load_many([ref])
        self.store.close()
        self.config.database_path.unlink()
        self.store = ConversationMediaStore(self.config, clock=lambda: NOW)
        with self.assertRaises(MediaUnavailableError):
            self.store.load_many([refs[0]])

    def test_aggregate_limits_preflight_before_pixel_loading(self):
        refs = [record['image_ref'] for record in self.retain()]
        for limits in (ImageLimits(max_images=1), ImageLimits(max_total_bytes=10), ImageLimits(max_total_pixels=50)):
            with self.assertRaises(ValueError):
                self.store.load_many(refs, limits=limits)
        with self.assertRaises(ValueError):
            self.retain(limits=ImageLimits(max_images=1))

    def test_history_and_request_linkage_without_successful_turn(self):
        records = self.retain()
        refs = [record['image_ref'] for record in records]
        self.store.link_request(refs, 'REQ'+'1'*32)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM request_media').fetchone()[0], 2)
        history = SQLiteConversationHistoryStore(Path(self.temp.name)/'history.sqlite3')
        try:
            history.begin_request('REQ'+'1'*32, 'session', NOW, InteractionChannel.REMOTE_TEXT, 'dialogue')
            turn = history.append(NewConversationTurn('session', NOW, InteractionChannel.REMOTE_TEXT,
                '', 'two photos', 'REQ'+'1'*32, tuple(refs)))
            self.assertEqual(turn.image_refs, tuple(refs))
            selected = history.select_prior_session('new-session', InteractionChannel.CONSOLE)
            self.assertEqual(selected[0].image_refs, tuple(refs))
            self.assertEqual(history.search('photos')['matches'][0]['image_refs'], refs)
        finally:
            history.close()

    def test_no_decoding_on_metadata_queries(self):
        self.retain()
        with patch('embodied_runtime.conversation_media.validate_image', side_effect=AssertionError('pixels loaded')):
            self.assertEqual(len(self.store.list_images()['images']), 2)

    def test_cleanup_never_expires_another_scope(self):
        refs = [record['image_ref'] for record in self.retain()]
        other = ConversationMediaStore(replace(self.config, operator_scope='someone_else', max_age_days=1),
                                       clock=lambda: NOW + timedelta(days=2))
        try:
            self.assertEqual(other.cleanup(), 0)
            self.assertEqual(len(self.store.load_many(refs)), 2)
        finally:
            other.close()

    def test_cleanup_is_bounded_and_metadata_cache_avoids_redecoding(self):
        self.retain()
        with patch('embodied_runtime.jobs.images.validate_image', side_effect=AssertionError('redundant decode')):
            self.retain()
            ImageLimits().validate(self.images, decode=True)
            ImageLimits().validate((self.images[0].with_context(label='new label'),), decode=True)
        self.now += timedelta(days=8)
        self.assertEqual(self.store.cleanup(limit=1), 1)
        self.assertEqual(self.store._db.execute('SELECT count(*) FROM pixels').fetchone()[0], 1)

    def test_invalid_configuration(self):
        for values in ({'max_items': 0}, {'max_age_days': True}, {'max_storage_bytes': -1},
                       {'operator_scope': '../operator'}, {'enabled': 'yes'}):
            with self.assertRaises(ValueError):
                replace(self.config, **values)
