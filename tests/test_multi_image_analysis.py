"""Joint requests, immutable fallback inputs and mixed-source operator orchestration."""

import asyncio
import base64
from dataclasses import replace
from datetime import UTC, datetime
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from embodied_runtime.app import CAMERA_RESOURCE, RobotApplication
from embodied_runtime.attachments import ImageAttachment
from embodied_runtime.cognition import CognitionToolCall, TextCognitionBackend
from embodied_runtime.cognition.openai_responses import OpenAIResponsesBackend
from embodied_runtime.cognition.openai_compatible import OpenAICompatibleBackend
from embodied_runtime.config import VisionConfiguration, load_runtime_config, ConfigurationError
from embodied_runtime.conversation_history import SQLiteConversationHistoryStore
from embodied_runtime.conversation_media import ConversationMediaStore, MediaRetentionConfiguration
from embodied_runtime.hardware.virtual import VirtualHardwareBackend
from embodied_runtime.images import ImageCollection, ImageInput, ImageLimits
from embodied_runtime.interaction import CONSOLE_DIALOGUE, VOICE_DIALOGUE
from embodied_runtime.jobs import FilesystemJobWorkspaceStore, SQLiteJobStore
from embodied_runtime.perception.factory import create_visual_perception_backend
from embodied_runtime.perception.visual import VisualPerceptionBackend, VisualPerceptionResult
from embodied_runtime.profile import RobotProfile
from tests.test_cognition import FakeResponses
from tests.test_openai_compatible import CONFIG as COGNITION_CONFIG, FakeCompletions, client, response
from tests.test_operator_image_delivery import Camera, Platform, SMS_DIALOGUE
from tests.test_portable_visual_perception import CONFIG, direct_client, direct_response
from tests.test_workspace_images import raster, provenance

NOW = datetime(2026, 10, 18, 15, tzinfo=UTC)


def selection(count=4):
    return ImageCollection(tuple(ImageInput(raster(color=color), 'image/jpeg', 8, 6,
        f'image-{index}', f'frame {index}', ('camera', 'workspace', 'sms', 'conversation_media')[index],
        NOW if index == 0 else None, NOW if index >= 2 else None)
        for index, color in enumerate(('red', 'blue', 'green', 'yellow')[:count])))


def pixels(parts):
    return [base64.b64decode((part['image_url']['url'] if isinstance(part['image_url'], dict)
            else part['image_url']).split(',')[1]) for part in parts if part['type'] in ('image_url', 'input_image')]


class MultiImageProviderTests(unittest.IsolatedAsyncioTestCase):
    async def test_four_ordered_images_same_question_and_labels_in_both_payloads(self):
        original = selection()
        first = FakeCompletions(ConnectionError('private'))
        second = FakeCompletions(direct_response())
        backend = create_visual_perception_backend('configured', CONFIG,
            clients=(client(first), direct_client(second)))
        await backend.interpret(original, 'What visible action is suggested by this ordered sequence?')
        a = first.requests[0]['messages'][1]['content']
        b = second.requests[0]['input'][0]['content']
        expected = [image.data for image in original.images]
        self.assertEqual(pixels(a), expected)
        self.assertEqual(pixels(b), expected)
        self.assertEqual(a[0]['text'], b[0]['text'])
        self.assertEqual([part['text'] for part in a if part['type'] == 'text'],
                         [part['text'] for part in b if part['type'] == 'input_text'])
        for index in range(4):
            metadata = json.loads(a[1 + index*2]['text'])
            self.assertEqual(metadata['image_ref'], f'image-{index}')
            self.assertEqual(metadata['label'], f'frame {index}')
            if index:
                self.assertIsNone(metadata['captured_at'])
        self.assertIn('not continuous', first.requests[0]['messages'][0]['content'])
        self.assertIn('lighting', second.requests[0]['instructions'])

    async def test_cognition_four_parts_and_single_image_payload_compatibility(self):
        attachments = tuple(ImageAttachment(image.media_type, image.data, image.reference, image.label)
                            for image in selection().images)
        responses = FakeResponses()
        direct = OpenAIResponsesBackend(client=SimpleNamespace(responses=responses))
        await direct.respond('compare these', image_attachments=attachments)
        self.assertEqual(pixels(responses.calls[0]['input'][0]['content']), [image.data for image in attachments])
        compatible_fake = FakeCompletions(response())
        compatible = OpenAICompatibleBackend(config=replace(COGNITION_CONFIG, image_input=True),
                                             client=client(compatible_fake))
        await compatible.respond('compare these', image_attachments=attachments)
        self.assertEqual(pixels(compatible_fake.requests[0]['messages'][0]['content']), [image.data for image in attachments])
        await direct.respond('one', image_attachments=(ImageAttachment('image/jpeg', raster()),))
        self.assertEqual([part['type'] for part in responses.calls[1]['input'][0]['content']],
                         ['input_text', 'input_image'])

    async def test_supported_openai_sdk_serializes_both_joint_formats(self):
        try:
            from openai import AsyncOpenAI
        except ImportError:
            self.skipTest("optional OpenAI SDK not installed")
        try:
            import httpx2 as httpx
        except ImportError:
            import httpx
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            payload = direct_response() if request.url.path.endswith('/responses') else response()
            return httpx.Response(200, json=payload)

        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        sdk = AsyncOpenAI(api_key='offline-test', base_url='https://provider.invalid/v1', http_client=http_client)
        try:
            backend = create_visual_perception_backend('configured', CONFIG, clients=(sdk, sdk))
            images = selection()
            await backend.backends[0].interpret(images, 'compare four')
            await backend.backends[1].interpret(images, 'compare four')
            self.assertEqual(pixels(requests[0]['messages'][1]['content']), [image.data for image in images.images])
            self.assertEqual(pixels(requests[1]['input'][0]['content']), [image.data for image in images.images])
        finally:
            await sdk.close()

    async def test_configured_count_byte_and_pixel_limits_before_provider(self):
        original = selection()
        for limits in (ImageLimits(max_images=3), ImageLimits(max_total_bytes=100), ImageLimits(max_total_pixels=100)):
            first = FakeCompletions(response())
            second = FakeCompletions(direct_response())
            backend = create_visual_perception_backend('configured', replace(CONFIG, image_limits=limits),
                clients=(client(first), direct_client(second)))
            with self.assertRaises(ValueError):
                await backend.interpret(original, 'compare')
            self.assertEqual(first.requests + second.requests, [])
        fake = FakeResponses()
        direct = OpenAIResponsesBackend(client=SimpleNamespace(responses=fake), image_limits=ImageLimits(max_images=2))
        image = ImageAttachment('image/jpeg', raster())
        with self.assertRaisesRegex(ValueError, 'maximum'):
            await direct.respond('look', image_attachments=(image,)*3)
        self.assertEqual(fake.calls, [])

    async def test_unsupported_cognition_capability_and_invalid_provider(self):
        fake = FakeCompletions(response())
        backend = OpenAICompatibleBackend(config=COGNITION_CONFIG, client=client(fake))
        with self.assertRaisesRegex(Exception, 'does not support image'):
            await backend.respond('look', image_attachments=(ImageAttachment('image/jpeg', raster()),)*2)
        self.assertEqual(fake.requests, [])
        with self.assertRaises(ConfigurationError):
            create_visual_perception_backend('unknown', CONFIG)

    def test_config_validation_and_inherited_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'runtime.toml'
            path.write_text('[vision]\nmax_images=2\nmax_total_bytes=12345\nmax_total_pixels=123456\n'
                            '[conversation_media]\nenabled=false\nmax_age_days=2\n')
            config = load_runtime_config(path)
            self.assertEqual(config.vision.image_limits, ImageLimits(2, 12345, 123456))
            self.assertFalse(config.conversation_media.enabled)
            for value in ('0', '17', 'true', '2.5'):
                path.write_text('[vision]\nmax_images='+value)
                with self.assertRaises(ConfigurationError):
                    load_runtime_config(path)


class JointVision(VisualPerceptionBackend):
    identifier = 'test-joint-vision'
    supports_multi_image = True

    def __init__(self):
        self.inputs = []
        self.before = None

    async def interpret(self, images, focus):
        if self.before:
            self.before()
        self.inputs.append((images, focus))
        return VisualPerceptionResult(focus, 'Visible differences may reflect lighting or viewpoint.')


class Script(TextCognitionBackend):
    identifier = 'test-multi-image-script'
    supports_image_input = True

    def __init__(self, steps=(), fail=False):
        self.steps, self.results, self.requests = list(steps), [], []
        self.fail = fail

    async def respond(self, message, *, tools=(), tool_executor=None, **kwargs):
        self.requests.append((message, tuple(tool.name for tool in tools), kwargs))
        if self.fail:
            raise RuntimeError('analysis failed')
        if self.steps:
            name, args = self.steps.pop(0)
            if callable(args):
                args = args(self.results)
            result = await tool_executor(CognitionToolCall(name, json.dumps(args)))
            self.results.append(json.loads(result.output))
        return 'analysis complete'


class MultiImageRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = MediaRetentionConfiguration(database_path=root/'media.sqlite3')
        self.media = ConversationMediaStore(self.config, clock=lambda: NOW)
        self.history = SQLiteConversationHistoryStore(root/'conversation.sqlite3')
        self.jobs = SQLiteJobStore(root/'jobs.sqlite3')
        self.workspace = FilesystemJobWorkspaceStore(root/'workspace')
        self.vision = JointVision()
        self.camera = Camera()
        self.camera.frames = [raster(color='yellow')]
        self.app = None

    async def asyncTearDown(self):
        if self.app:
            await self.app.stop()
        self.workspace.close()
        self.jobs.close()
        self.history.close()
        self.media.close()
        self.temp.cleanup()

    async def start(self, cognition, media=True, limits=ImageLimits(), vision=True):
        # RobotApplication owns and closes injected stores on shutdown.
        root = Path(self.temp.name)
        if self.app is not None:
            self.jobs = SQLiteJobStore(root/'jobs.sqlite3')
            self.workspace = FilesystemJobWorkspaceStore(root/'workspace')
            self.history = SQLiteConversationHistoryStore(root/'conversation.sqlite3')
        self.app = RobotApplication(RobotProfile('test', 'Test'), VirtualHardwareBackend(),
            platform_provider=Platform(), cognition_backend=cognition, camera_backend=self.camera,
            visual_perception_backend=self.vision if vision else None,
            conversation_history_store=self.history, conversation_media_store=self.media if media else None,
            image_limits=limits, wall_clock=lambda: NOW, job_store=self.jobs, job_workspace_store=self.workspace)
        await self.app.start()
        return self.app

    def retained(self, identity='SM1'):
        attachments = (ImageAttachment('image/jpeg', raster(color='blue'), source='sms',
            received_at=NOW, source_message_id=identity),)
        return self.media.retain(attachments, channel='remote_text', message_identity=identity,
                                 message_text='yesterday plant photo')[0]['image_ref']

    async def test_four_mixed_sources_one_acquisition_camera_once_lease_released(self):
        ref = self.retained()
        job = self.jobs.create_job('baseline')
        baseline = raster(color='green')
        self.workspace.save_image(job.id, 'photos/baseline', 'create', baseline, provenance(job.id))
        selectors = [ref, 'attachment:1', f'workspace:JOB{job.id}:photos/baseline', 'camera']
        script = Script([('interpret_image', {'image_refs': selectors, 'focus': 'Compare all four.'})])
        app = await self.start(script)
        self.vision.before = lambda: self.assertIsNone(app.resources.lease_for(CAMERA_RESOURCE))
        attached = ImageAttachment('image/png', raster('PNG', 'red'))
        await app.request_cognition('Compare these four.', interaction=CONSOLE_DIALOGUE, image_attachments=(attached,))
        self.assertEqual(script.results[0]['status'], 'applied', script.results)
        images = self.vision.inputs[0][0].images
        self.assertEqual([image.data for image in images], [raster(color='blue'), attached.data, baseline, self.camera.frames[0]])
        self.assertEqual(self.camera.captures, 1)
        self.assertEqual(script.results[0]['image_count'], 4)
        self.assertEqual(len(script.requests), 2)
        self.assertIsNone(images[0].captured_at)
        self.assertIsNotNone(images[-1].captured_at)

    async def test_restart_cross_channel_listing_then_reanalysis_in_two_acquisitions(self):
        script = Script()
        app = await self.start(script)
        photos = tuple(ImageAttachment('image/jpeg', raster(color=color), source_message_id='SM1',
            attachment_index=index, source='sms', received_at=NOW) for index, color in enumerate(('red', 'blue')))
        await app.request_cognition('plant photos', interaction=SMS_DIALOGUE, image_attachments=photos)
        old_refs = self.media.list_images()['images']
        await app.stop()
        self.media.close()
        self.media = ConversationMediaStore(self.config, clock=lambda: NOW)
        second = Script([('list_conversation_images', {'query': 'plant', 'limit': 2}),
            ('interpret_image', lambda results: {'image_refs': [row['image_ref'] for row in results[0]['images']],
                                                'focus': 'Compare the last two photos.'})])
        app = await self.start(second)
        await app.request_cognition('Compare the last two plant photos I sent.', interaction=VOICE_DIALOGUE)
        self.assertEqual(second.results[-1]['status'], 'applied')
        self.assertEqual(second.results[-1]['image_refs'], [row['image_ref'] for row in old_refs])
        self.assertEqual(len(second.requests), 3)
        self.assertNotIn('interpret_image', second.requests[-1][1])
        self.assertEqual(self.camera.captures, 0)
        self.assertNotIn('image_attachments', second.requests[0][2])
        self.assertEqual(self.history.select_prior_session('other', SMS_DIALOGUE.channel)[0].image_refs,
                         tuple(row['image_ref'] for row in old_refs))

    async def test_image_only_and_failed_analysis_still_discoverable_and_linked(self):
        app = await self.start(Script(fail=True))
        with self.assertRaisesRegex(RuntimeError, 'analysis failed'):
            await app.request_cognition('', interaction=SMS_DIALOGUE,
                image_attachments=(ImageAttachment('image/jpeg', raster()),))
        records = self.media.list_images()['images']
        self.assertEqual(len(records), 1)
        self.assertEqual(self.media._db.execute('SELECT count(*) FROM request_media').fetchone()[0], 1)
        self.assertEqual(self.history.select_prior_session('other', SMS_DIALOGUE.channel), ())
        self.assertEqual(self.media.load_many([records[0]['image_ref']])[0].data, raster())

    async def test_retention_failure_preserves_current_analysis_and_reports_not_saved(self):
        script = Script([('interpret_image', {'image_refs': ['attachment:1'], 'focus': 'Describe.'})])
        app = await self.start(script)
        with patch.object(self.media, 'retain', side_effect=OSError('disk full')):
            response_text = await app.request_cognition('look', interaction=CONSOLE_DIALOGUE,
                image_attachments=(ImageAttachment('image/jpeg', raster()),))
        self.assertEqual(script.results[0]['status'], 'applied')
        self.assertIn('not saved', response_text)
        self.assertEqual(self.media.list_images()['images'], [])
        self.assertIn('failed_not_saved', script.requests[0][2]['instructions'])

    async def test_count_rejection_before_retention_and_camera(self):
        app = await self.start(Script(), limits=ImageLimits(max_images=2))
        image = ImageAttachment('image/jpeg', raster())
        with self.assertRaisesRegex(ValueError, 'maximum'):
            await app.request_cognition('compare', image_attachments=(image,)*3)
        result = json.loads((await app._execute_image_interpretation(CognitionToolCall('interpret_image',
            json.dumps({'image_refs': ['camera']*3, 'focus': 'compare'})), {}, 1)).output)
        self.assertEqual(result['status'], 'rejected')
        self.assertEqual(self.camera.captures, 0)
        self.assertEqual(self.media.list_images()['images'], [])

    async def test_missing_reference_never_substitutes_camera_and_delete_command(self):
        ref = self.retained()
        script = Script([('interpret_image', {'image_refs': ['MEDIA'+'0'*32, 'camera'], 'focus': 'compare'})])
        app = await self.start(script)
        await app.request_cognition('compare the old photo with now', interaction=CONSOLE_DIALOGUE)
        self.assertEqual(script.results[0]['status'], 'rejected')
        self.assertEqual(self.camera.captures, 0)
        response_text = await app.request_cognition('delete conversation images '+ref, interaction=CONSOLE_DIALOGUE)
        self.assertIn('Deleted', response_text)
        self.assertEqual(self.media.list_images()['images'], [])

    async def test_legacy_store_disabled_and_ambiguous_matches_do_not_load_pixels(self):
        ref = self.retained()
        self.retained('SM2')
        script = Script([('list_conversation_images', {'query': 'plant'})])
        app = await self.start(script)
        with patch.object(self.media, 'load_many', side_effect=AssertionError('unexpected pixels')):
            await app.request_cognition('the plant photo', interaction=CONSOLE_DIALOGUE)
        self.assertEqual(len(script.results[0]['images']), 2)
        self.assertIn('Ask for clarification', script.requests[0][2]['instructions'])
        self.assertEqual(self.vision.inputs, [])
        await app.stop()
        script = Script([('list_conversation_images', {})])
        app = await self.start(script, media=False)
        await app.request_cognition('that old photo', interaction=CONSOLE_DIALOGUE)
        self.assertEqual(script.results[0]['status'], 'unavailable')

    async def test_low_configured_budget_applies_to_live_scene_and_workspace_load(self):
        job = self.jobs.create_job('baseline')
        self.workspace.save_image(job.id, 'frame', 'create', raster(), provenance(job.id))
        script = Script()
        app = await self.start(script, limits=ImageLimits(max_total_bytes=10))
        captured = json.loads(app._execute_camera_image_capture(
            CognitionToolCall('capture_camera_image', '{}'), {}, 1).output)
        self.assertEqual(captured['status'], 'rejected')
        loaded = json.loads(app._execute_workspace_image(CognitionToolCall('workspace_load_image',
            json.dumps({'job': f'JOB{job.id}', 'path': 'frame'})), {}, 1).output)
        self.assertEqual(loaded['status'], 'rejected')
        result, _ = await app._execute_visual_perception(CognitionToolCall('observe_scene', '{"focus":"look"}'))
        self.assertEqual(json.loads(result.output)['status'], 'rejected')
        self.assertEqual(self.vision.inputs, [])

    async def test_camera_once_and_released_before_each_fallback_attempt(self):
        script = Script([('interpret_image', {'image_refs': ['camera', 'camera'], 'focus': 'Compare.'})])
        app = await self.start(script)

        async def fail():
            self.assertIsNone(app.resources.lease_for(CAMERA_RESOURCE))
            raise ConnectionError('offline')

        async def succeed():
            self.assertIsNone(app.resources.lease_for(CAMERA_RESOURCE))
            return direct_response()

        first, second = FakeCompletions(fail), FakeCompletions(succeed)
        app._visual_perception_backend = create_visual_perception_backend('configured', CONFIG,
            clients=(client(first), direct_client(second)))
        await app.request_cognition('Compare the selected camera images.', interaction=CONSOLE_DIALOGUE)
        self.assertEqual(script.results[0]['status'], 'applied')
        self.assertEqual(self.camera.captures, 1)
        a, b = first.requests[0]['messages'][1]['content'], second.requests[0]['input'][0]['content']
        self.assertEqual(pixels(a), [self.camera.frames[0]]*2)
        self.assertEqual(pixels(a), pixels(b))
        self.assertEqual(a[0]['text'], b[0]['text'])

    async def test_unsupported_local_multi_image_backend_does_not_split(self):
        self.vision.supports_multi_image = False
        script = Script([('interpret_image', {'image_refs': ['attachment:1', 'attachment:2'], 'focus': 'compare'})])
        app = await self.start(script)
        image = ImageAttachment('image/jpeg', raster())
        await app.request_cognition('compare', image_attachments=(image,image))
        self.assertEqual(script.results[0]['reason'], 'unsupported_capability')
        self.assertEqual(self.vision.inputs, [])
