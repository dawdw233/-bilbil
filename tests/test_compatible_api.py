"""Local HTTP servers exercise compatible protocols without paid cloud calls."""
import base64
import io
import json
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, patch

from aiohttp import web
from PIL import Image
from test_appearance import plugin
from test_auto_publish import PLAN

auto=import_module(plugin.__package__+".auto_publish")
voice=import_module(plugin.__package__+".voiceover")
speech=import_module(plugin.__package__+".speech_recognition")
urls=import_module(plugin.__package__+".api_config")


class CompatibleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);(self.root/"media").mkdir()
        self.now=plugin.datetime(2026,11,8,18,tzinfo=plugin.BEIJING_TIME)

    async def server(self, routes):
        app=web.Application();app.add_routes(routes)
        runner=web.AppRunner(app);await runner.setup();self.addAsyncCleanup(runner.cleanup)
        site=web.TCPSite(runner,"127.0.0.1",0);await site.start()
        return "http://127.0.0.1:"+str(site._server.sockets[0].getsockname()[1])+"/v1"

    def worker(self):
        return auto.AutoPublisher(self.root,AsyncMock(return_value=PLAN),AsyncMock(return_value={"success":True,"bvid":"BV1234567890"}),lambda:"42",lambda:False)

    async def test_endpoint_validation_and_normalization(self):
        self.assertEqual(urls.api_base("https://example.test/v1/"),"https://example.test/v1")
        self.assertEqual(urls.api_base("https://example.test/ws/",websocket=True),"wss://example.test/ws")
        for value in ([],"file:///key","https://user:key@example.test/v1","https://example.test/?api_key=key","https://example.test/#secret","https://example.test:bad","https://example.test/ bad"):
            with self.subTest(value=value),self.assertRaises(ValueError):urls.api_base(value)

    async def test_asr_migrates_legacy_key_and_isolates_profiles_and_pinned_endpoint(self):
        path=self.root/"voiceover_settings.json"
        path.write_text(json.dumps({"asr_workspace":"draft-space","asr_key":""}))
        self.assertEqual(voice.VoiceoverService(self.root,AsyncMock()).settings()["asr_workspace"],"draft-space")
        path.write_text(json.dumps({"enabled":True,"asr_key":"legacy-asr","asr_workspace":"space-1"}))
        service=voice.VoiceoverService(self.root,AsyncMock())
        old=service.pin()
        service.save({"asr_service":"openai","asr_api_key":"other-asr","asr_base_url":"https://gateway.test/v1","asr_model":"my-whisper"})
        self.assertEqual(service.asr_key(old),"legacy-asr")
        new=service.pin();self.assertEqual(new["asr_base_url"],"https://gateway.test/v1")
        service.save({"asr_base_url":"https://changed.test/v1","asr_model":"new-model"})
        self.assertEqual(new["asr_base_url"],"https://gateway.test/v1")
        service.save({"asr_service":"paraformer"})
        self.assertEqual(service.settings()["asr_workspace"],"space-1")
        self.assertEqual(service.asr_key(new),"other-asr")
        self.assertNotIn("legacy-asr",json.dumps(service.state()))
        self.assertNotIn("other-asr",json.dumps(service.state()))
        self.assertNotIn('"asr_key"',path.read_text())

    async def test_actual_http_asr_multipart_wav_custom_model_and_timestamp_requirement(self):
        mode="segments";calls=[]
        async def recognize(request):
            data=await request.post();calls.append((request.headers.get("Authorization"),data["model"],data["response_format"]))
            self.assertEqual(data["file"].file.read(4),b"RIFF")
            self.assertEqual(data["timestamp_granularities[]"],"segment")
            return web.json_response({"segments":[{"start":.1,"end":.8,"text":"真实音轨文字"}]} if mode=="segments" else {"text":"没有时间戳"})
        base=await self.server([web.post('/v1/audio/transcriptions',recognize)])
        client=speech.OpenAIRecognitionClient("asr-only-key",base_url=base,model="custom-whisper")
        self.assertEqual((await client.recognize(b"\0\0"*16000,1,"a"*32))[0]["start"],.1)
        self.assertEqual(calls[0],("Bearer asr-only-key","custom-whisper","verbose_json"))
        mode="text"
        with self.assertRaisesRegex(speech.RecognitionError,"segments"):
            await client.recognize(b"\0\0"*16000,1,"b"*32)

    async def test_video_http_full_pipeline_keeps_original_endpoint_after_profile_edit(self):
        calls=[]
        async def models(request):return web.json_response({"data":[{"id":"gateway/video-model"}]})
        async def create(request):
            body=await request.post();calls.append(("create",body["model"],body["seconds"],request.content_type))
            return web.json_response({"id":"job-1","status":"queued"})
        async def query(request):calls.append(("query",request.match_info["id"]));return web.json_response({"status":"completed"})
        async def content(request):
            self.assertEqual(request.headers["Authorization"],"Bearer video-only-key")
            calls.append(("content",request.match_info["id"]))
            return web.Response(body=b"0000ftyp"+b"0"*2048,content_type="video/mp4")
        base=await self.server([web.get('/v1/models',models),web.post('/v1/videos',create),web.get('/v1/videos/{id}',query),web.get('/v1/videos/{id}/content',content)])
        worker=self.worker()
        worker.save_settings({"enabled":True,"provider":"openai","region":"custom","api_key":"video-only-key","base_url":base,"model":"gateway/video-model","duration":4},self.now)
        self.assertEqual((await worker.models({}))["models"][0]["id"],"gateway/video-model")
        job=worker.enqueue(self.now,manual=True)
        await worker.tick(self.now)
        worker.save_settings({"base_url":"http://127.0.0.1:1/v1"},self.now)
        for _ in range(5):
            await worker.tick(self.now)
            if worker.jobs()[0]["phase"] in ("published","failed","review"):break
        self.assertEqual(worker.jobs()[0]["phase"],"published")
        self.assertEqual(worker.jobs()[0]["base_url"],base)
        self.assertEqual(calls[0],("create","gateway/video-model","4","multipart/form-data"))
        self.assertIn(("content","job-1"),calls)
        worker.publish.assert_awaited_once()
        self.assertNotIn("video-only-key",json.dumps(worker.state()))

    async def test_json_video_reference_is_resized_and_failures_are_not_recreated(self):
        captured=[]
        async def create(request):captured.append(await request.json());return web.json_response({"id":"task","status":"queued"})
        async def query(request):return web.json_response({"status":"failed","error":{"message":"do not expose"}})
        base=await self.server([web.post('/v1/videos',create),web.get('/v1/videos/task',query)])
        image=io.BytesIO();Image.new("RGB",(600,600),"blue").save(image,format="PNG")
        client=auto.CompatibleVideo("openai","custom","video-key","alias",base_url=base,transport="json")
        await client.create("角色动作",reference_image="data:image/png;base64,"+base64.b64encode(image.getvalue()).decode())
        raw=base64.b64decode(captured[0]["input_reference"]["image_url"].split(',')[1])
        with Image.open(io.BytesIO(raw)) as result:self.assertEqual(result.size,(1280,720))
        with self.assertRaises(auto.GenerationFailed) as error:await client.query("task")
        self.assertNotIn("do not expose",str(error.exception))
        self.assertEqual(len(captured),1)

    async def test_native_adapters_accept_custom_base_and_keys_remain_per_provider(self):
        worker=self.worker()
        worker.save_settings({"enabled":True,"api_key":"minimax-key","base_url":"https://minimax-proxy.test/v1","model":"custom-alias"},self.now)
        worker.save_settings({"provider":"ark","region":"cn","api_key":"ark-key","base_url":"https://ark-proxy.test/api/v3","model":"endpoint-alias"},self.now)
        self.assertEqual(worker.settings()["profiles"]["minimax:cn"]["key"],"minimax-key")
        job=worker.enqueue(self.now,manual=True)
        self.assertEqual(job["base_url"],"https://ark-proxy.test/api/v3")
        with patch.object(auto.ArkVideo,"_request",AsyncMock(return_value={"id":"task"})):
            await worker.tick(self.now)
        self.assertEqual(worker.jobs()[0]["phase"],"generating")

    async def test_compatible_video_requires_explicit_model_address_and_legal_duration(self):
        worker=self.worker()
        for patch_data in ({"base_url":""},{"base_url":"https://gateway.test/v1","model":""},{"base_url":"https://gateway.test/v1","model":"video","duration":7}):
            with self.subTest(patch_data=patch_data),self.assertRaises(auto.PublishError):
                worker.save_settings({"enabled":True,"provider":"openai","region":"custom","api_key":"video-key",**patch_data},self.now)
        self.assertEqual(auto.storyboard_lengths("openai","video",60),[12]*5)
        # 加了 10 秒档之后，30 秒也能凑成 3 段
        self.assertEqual(auto.storyboard_lengths("openai","video",30),[12,10,8])
