"""Ordered MMS ingress, signed envelope bounds and durable retry identities."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from embodied_runtime.app import RobotApplication
from embodied_runtime.attachments import ImageAttachment
from embodied_runtime.conversation_history import SQLiteConversationHistoryStore
from embodied_runtime.conversation_media import ConversationMediaStore, MediaRetentionConfiguration
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.images import ImageLimits
from embodied_runtime.profile import RobotProfile
from embodied_runtime.sms import TwilioSmsService, MediaDownloadError
from tests.test_multi_image_analysis import Script, NOW
from tests.test_operator_image_delivery import Platform
from tests.test_sms import settings, media_form
from tests.test_workspace_images import raster


def form(count=2, sid='SM1', body='Compare these photos'):
    payload = media_form(MessageSid=sid, Body=body, NumMedia=str(count))
    for index in range(count):
        payload[f'MediaUrl{index}'] = f'https://api.twilio.com/test/{index}'
        payload[f'MediaContentType{index}'] = 'image/jpeg'
    return payload


class Downloader:
    def __init__(self):
        self.calls = []
        self.fail_at = None

    async def download(self, reference):
        index = len(self.calls)
        self.calls.append(reference)
        if index == self.fail_at:
            raise MediaDownloadError('media_download_failed')
        return ImageAttachment('image/jpeg', raster(color=('red', 'blue', 'green', 'yellow')[int(reference.url.rsplit('/', 1)[1])]))


class MultiImageSmsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.media_config = MediaRetentionConfiguration(database_path=root/'media.sqlite3')
        self.media = ConversationMediaStore(self.media_config)
        self.history = SQLiteConversationHistoryStore(root/'conversation.sqlite3')
        self.script = Script()
        self.app = RobotApplication(RobotProfile('test', 'Test'), VirtualHardwareBackend(),
            platform_provider=Platform(), cognition_backend=self.script,
            conversation_history_store=self.history, conversation_media_store=self.media)
        await self.app.start()
        self.downloader = Downloader()
        self.gateway = Mock()
        self.service = self.make_service()

    def make_service(self, limits=ImageLimits()):
        service = TwilioSmsService(settings(), self.app.request_cognition,
            gateway=self.gateway, media_downloader=self.downloader, image_limits=limits, media_store=self.media)
        service._accepting = True
        service._worker = asyncio.create_task(service._run_worker())
        return service

    async def asyncTearDown(self):
        await self.service.stop()
        await self.app.stop()
        self.media.close()
        self.temp.cleanup()

    async def restart_service(self):
        await self.service.stop()
        self.media.close()
        self.media = ConversationMediaStore(self.media_config)
        self.app.conversation_media = self.media
        self.service = self.make_service()

    async def drain(self):
        await asyncio.wait_for(self.service._inbox.join(), 2)

    async def test_four_attachments_preserve_order_image_only_and_duplicates(self):
        payload = form(4, body='')
        self.assertEqual(self.service._accept_validated_form(payload).reason, 'accepted')
        self.assertEqual(self.service._accept_validated_form(payload).reason, 'duplicate')
        await self.drain()
        attachments = self.script.requests[0][2]['image_attachments']
        self.assertEqual([image.attachment_index for image in attachments], [0,1,2,3])
        self.assertEqual([image.data for image in attachments],
                         [raster(color=color) for color in ('red','blue','green','yellow')])
        records = self.media.list_images()['images']
        self.assertEqual([record['attachment_index'] for record in records], [0,1,2,3])
        self.assertTrue(all(record['captured_at'] is None for record in records))
        self.assertEqual(len(self.downloader.calls), 4)
        self.gateway.send.assert_called_once()

    async def test_retry_after_restart_does_not_download_or_reanalyze(self):
        payload = form(2)
        self.service._accept_validated_form(payload)
        await self.drain()
        await self.service.stop()
        self.media.close()
        self.media = ConversationMediaStore(self.media_config)
        self.app.conversation_media = self.media
        self.service = self.make_service()
        self.assertEqual(self.service._accept_validated_form(payload).reason, 'duplicate')
        await self.drain()
        self.assertEqual(len(self.downloader.calls), 2)
        self.assertEqual(len(self.script.requests), 1)
        self.assertEqual(len(self.media.list_images()['images']), 2)

    async def test_analysis_failure_keeps_all_images_discoverable(self):
        self.script.fail = True
        self.service._accept_validated_form(form(2, body=''))
        await self.drain()
        self.assertEqual(len(self.media.list_images()['images']), 2)
        self.assertEqual(self.media._db.execute('SELECT count(*) FROM request_media').fetchone()[0], 2)
        self.assertEqual(self.gateway.send.call_count, 0)
        await self.restart_service()
        self.assertEqual(self.service._accept_validated_form(form(2, body='')).reason, 'duplicate')
        self.assertEqual(len(self.script.requests), 1)

    async def test_partial_download_failure_keeps_valid_earlier_media(self):
        self.downloader.fail_at = 1
        self.service._accept_validated_form(form(2))
        await self.drain()
        self.assertEqual(len(self.media.list_images()['images']), 1)
        self.assertEqual(self.script.requests, [])
        self.gateway.send.assert_called_once()

    async def test_partial_retry_after_restart_recovers_only_missing_attachment_and_analyzes_once(self):
        payload = form(2)
        self.downloader.fail_at = 1
        self.service._accept_validated_form(payload)
        await self.drain()
        original = self.media.list_images()['images'][0]
        self.assertEqual(len(self.script.requests), 0)
        await self.service.stop()
        self.media.close()
        self.media = ConversationMediaStore(self.media_config)
        self.app.conversation_media = self.media
        self.service = self.make_service()
        self.downloader.fail_at = None
        self.assertEqual(self.service._accept_validated_form(payload).reason, 'accepted')
        await self.drain()
        self.assertEqual(len(self.script.requests), 1)
        photos = self.script.requests[0][2]['image_attachments']
        self.assertEqual([photo.data for photo in photos], [raster(color='red'), raster(color='blue')])
        records = self.media.list_images()['images']
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]['image_ref'], original['image_ref'])
        self.assertEqual(records[0]['received_at'], original['received_at'])
        self.assertEqual([reference.url.rsplit('/', 1)[1] for reference in self.downloader.calls], ['0', '1', '1'])
        self.assertEqual(self.service._accept_validated_form(payload).reason, 'duplicate')
        await self.service.stop()
        self.media.close()
        self.media = ConversationMediaStore(self.media_config)
        self.app.conversation_media = self.media
        self.service = self.make_service()
        self.assertEqual(self.service._accept_validated_form(payload).reason, 'duplicate')
        self.assertEqual(len(self.script.requests), 1)

    async def test_partial_retry_in_same_process_is_allowed_and_preserves_receipt_order(self):
        self.downloader.fail_at = 1
        payload = form(2)
        self.service._accept_validated_form(payload)
        await self.drain()
        self.assertEqual(self.media.ingestion_state('remote_text', 'SM1')['status'], 'partial')
        self.assertNotIn('SM1', self.service._accepted)
        self.downloader.fail_at = None
        self.assertEqual(self.service._accept_validated_form(payload).reason, 'accepted')
        self.assertEqual(self.service._accept_validated_form(payload).reason, 'duplicate')
        await self.drain()
        self.assertEqual(self.media.ingestion_state('remote_text', 'SM1')['status'], 'completed')
        records = self.media.list_images()['images']
        self.assertEqual([record['attachment_index'] for record in records], [0, 1])
        self.assertEqual(records[0]['received_at'], records[1]['received_at'])
        self.assertEqual(len(self.script.requests), 1)
        self.assertEqual(len(self.downloader.calls), 3)

    async def test_complete_downloads_before_analysis_claim_remain_recoverable(self):
        payload = form(2)
        self.media.begin_ingestion('remote_text', 'SM1', 2, datetime.now(UTC))
        photos = tuple(ImageAttachment('image/jpeg', raster(color=color), source='sms',
            source_message_id='SM1', attachment_index=index) for index, color in enumerate(('red', 'blue')))
        self.media.retain(photos, channel='remote_text', message_identity='SM1')
        await self.restart_service()
        self.assertEqual(self.service._accept_validated_form(payload).reason, 'accepted')
        await self.drain()
        self.assertEqual(self.downloader.calls, [])
        self.assertEqual(len(self.script.requests), 1)
        self.assertEqual(self.media.ingestion_state('remote_text', 'SM1')['status'], 'completed')

    async def test_indeterminate_analysis_is_not_repeated_after_restart(self):
        entered = asyncio.Event()

        async def uncertain(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        self.service._request_cognition = uncertain
        payload = form(2)
        self.service._accept_validated_form(payload)
        await asyncio.wait_for(entered.wait(), 2)
        self.assertEqual(self.media.ingestion_state('remote_text', 'SM1')['status'], 'processing')
        await self.restart_service()
        self.assertEqual(self.service._accept_validated_form(payload).reason, 'duplicate')
        self.assertEqual(self.script.requests, [])

    async def test_deleted_partial_attachment_is_not_downloaded_or_resurrected(self):
        self.downloader.fail_at = 1
        payload = form(2)
        self.service._accept_validated_form(payload)
        await self.drain()
        reference = self.media.list_images()['images'][0]['image_ref']
        self.media.delete([reference])
        await self.restart_service()
        self.downloader.fail_at = None
        self.assertEqual(self.service._accept_validated_form(payload).reason, 'accepted')
        await self.drain()
        self.assertEqual(len(self.downloader.calls), 2)
        self.assertEqual(self.script.requests, [])
        self.assertEqual(self.media.list_images()['images'], [])

    async def test_retention_failure_still_analyzes_current_message_once(self):
        payload = form(2)
        with patch.object(self.media, 'retain', side_effect=OSError('disk full')):
            self.service._accept_validated_form(payload)
            await self.drain()
        self.assertEqual(len(self.script.requests), 1)
        self.assertIn('not saved', self.gateway.send.call_args.kwargs['body'])
        self.assertEqual(self.media.list_images()['images'], [])
        await self.restart_service()
        self.assertEqual(self.service._accept_validated_form(payload).reason, 'duplicate')

    async def test_count_limit_and_missing_fields_reject_before_queue(self):
        self.assertEqual(self.service._accept_validated_form(form(5)).status, 413)
        payload = form(2)
        del payload['MediaUrl1']
        self.assertEqual(self.service._accept_validated_form(payload).status, 400)
        await self.service.stop()
        self.service = self.make_service(ImageLimits(max_images=1))
        self.assertEqual(self.service._accept_validated_form(form(2)).status, 413)
        self.assertEqual(self.downloader.calls, [])

    async def test_aggregate_limit_never_silently_analyzes_a_subset(self):
        await self.service.stop()
        self.service = self.make_service(ImageLimits(max_total_bytes=len(raster())+10))
        self.service._accept_validated_form(form(2))
        await self.drain()
        self.assertEqual(self.script.requests, [])
        self.assertEqual(len(self.downloader.calls), 2)
        self.assertEqual(len(self.media.list_images()["images"]), 1)
        self.gateway.send.assert_called_once()
