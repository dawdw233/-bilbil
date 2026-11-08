"""Mock paid APIs and exercise real FFmpeg narration/subtitle rendering."""
import asyncio
import io
import json
import os
import tempfile
import unittest
import wave
from datetime import datetime
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, patch

from test_appearance import plugin
from test_auto_publish import PLAN

voice = import_module(plugin.__package__ + ".voiceover")
auto = import_module(plugin.__package__ + ".auto_publish")


def wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1); stream.setsampwidth(2); stream.setframerate(24000)
        stream.writeframes(b"\0\0" * 12000)
    return output.getvalue()


class VoiceoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        (self.path / "media").mkdir()
        self.plan = AsyncMock(side_effect=lambda _job,slots:json.dumps({"segments":[{"text":"开饭啦。"} for _ in slots]}))
        self.service = voice.VoiceoverService(self.path,self.plan)
        self.now = datetime(2026,11,8,19,tzinfo=plugin.BEIJING_TIME)

    def configure(self, **extra):
        self.service.save({"enabled":True,"api_key":"speech-secret",**extra})

    def worker(self, provider="minimax"):
        self.configure()
        self.publish=AsyncMock(return_value={"success":True,"bvid":"BV1234567890"})
        self.worker_instance=auto.AutoPublisher(self.path,AsyncMock(return_value=PLAN),self.publish,lambda:"42",lambda:False,
                                              verify_publication=AsyncMock(return_value=False),voiceover=self.service)
        self.worker_instance.save_settings({"enabled":True,"api_key":"video-secret","provider":provider},self.now)
        return self.worker_instance

    def pipeline(self, provider="minimax"):
        self.create=AsyncMock(return_value="video-task")
        self.synthesize=AsyncMock(return_value={"content":wav()})
        async def rendered(job,*_args):
            final=self.service.path(job,".mp4")
            tmp=final.with_suffix(".done")
            tmp.write_bytes(b"voiced MP4");tmp.replace(final)
            return final
        self.render=AsyncMock(side_effect=rendered)
        for target,name,value in (
            (auto.VIDEO_CLIENTS[provider],"create",self.create),
            (auto.VIDEO_CLIENTS[provider],"query",AsyncMock(return_value="remote-file")),
            (auto.VIDEO_CLIENTS[provider],"download",AsyncMock(side_effect=lambda _id,_tmp,dest:dest.write_bytes(b"original MP4"))),
            (voice.SpeechClient,"synthesize",self.synthesize),
            (self.service,"render",self.render),
            (self.service,"preflight",AsyncMock()),
            (voice,"probe_video",AsyncMock(return_value={"duration":6,"audio":False})),
            (voice,"audio_duration",AsyncMock(return_value=.5)),
        ):
            item=patch.object(target,name,value);item.start();self.addCleanup(item.stop)

    async def drive(self, worker, until=("published","failed","review")):
        for _ in range(15):
            await worker.tick(self.now)
            job=worker.jobs()[0]
            if job["phase"] in until:return job
        self.fail("Pipeline did not reach expected state")

    async def test_settings_separate_keys_profiles_and_default_disabled(self):
        self.assertFalse(self.service.state()["settings"]["enabled"])
        self.configure()
        self.service.save({"provider":"qwen","api_key":"qwen-secret","voice":"Cherry","model":"qwen3-tts-flash"})
        self.service.save({"region":"global","api_key":"global-secret"})
        state=json.dumps(self.service.state())
        for secret in ("speech-secret","qwen-secret","global-secret"):self.assertNotIn(secret,state)
        self.assertEqual(self.service.settings()["profiles"]["minimax:cn"]["key"],"speech-secret")
        for payload in ({"enabled":"yes"},{"provider":[]},{"voice":[]},{"font_size":True},{"font_size":80},{"speed":float("nan")},{"voice_volume":0},{"mix":1}):
            with self.assertRaises(voice.VoiceoverError):self.service.save(payload)

    async def test_all_three_video_adapters_pass_through_same_post_processor(self):
        for provider in ("minimax","ark","wan"):
            with self.subTest(provider=provider):
                self.worker(provider)
                worker=self.worker_instance
                worker.jobs_path.unlink(missing_ok=True)
                self.pipeline(provider)
                worker.enqueue(self.now,manual=True)
                job=await self.drive(worker)
                self.assertEqual(job["phase"],"published")
                self.create.assert_awaited_once()
                self.synthesize.assert_awaited_once()
                self.render.assert_awaited_once()
                self.assertTrue(job["voiceover"]["complete"])
                self.assertEqual(Path(self.publish.await_args.args[0]["video_path"]).read_bytes(),b"voiced MP4")
                self.assertNotIn("speech-secret",json.dumps(worker.state()))
                self.assertNotIn("speech-secret",worker.jobs_path.read_text())

    async def test_uncertain_speech_request_is_never_retried(self):
        worker=self.worker();self.pipeline()
        self.synthesize.side_effect=TimeoutError()
        worker.enqueue(self.now,manual=True)
        job=await self.drive(worker)
        self.assertEqual(job["phase"],"review")
        await worker.tick(self.now)
        self.synthesize.assert_awaited_once()
        self.publish.assert_not_awaited()
        with self.assertRaises(auto.PublishError):worker.resume(job["id"])

    async def test_render_retry_reuses_original_video_and_finished_speech(self):
        worker=self.worker();self.pipeline()
        original=self.render.side_effect
        async def flaky(*args):
            if self.render.await_count==1:raise voice.CompositionError("local failure")
            return await original(*args)
        self.render.side_effect=flaky
        worker.enqueue(self.now,manual=True)
        job=await self.drive(worker)
        self.assertEqual((job["phase"],job["retry_stage"]),("failed","voice"))
        self.assertTrue(self.service.path(job,"-source.mp4").exists())
        worker.resume(job["id"])
        self.assertEqual((await self.drive(worker))["phase"],"published")
        self.create.assert_awaited_once();self.synthesize.assert_awaited_once()

    async def test_interrupted_speech_submission_stays_review_without_resubmission(self):
        worker=self.worker();self.pipeline()
        self.synthesize.side_effect=asyncio.CancelledError()
        worker.enqueue(self.now,manual=True)
        for _ in range(3):await worker.tick(self.now)
        with self.assertRaises(asyncio.CancelledError):await worker.tick(self.now)
        self.assertEqual(worker.jobs()[0]["voiceover"]["clips"][0]["phase"],"requesting")
        await worker.tick(self.now)
        self.assertEqual(worker.jobs()[0]["phase"],"review")
        self.synthesize.assert_awaited_once()

    async def test_download_retry_reuses_original_url_without_another_paid_call(self):
        worker=self.worker();self.pipeline()
        self.synthesize.return_value={"url":"https://bucket.oss-cn-beijing.aliyuncs.com/voice.wav?private=signature"}
        with patch.object(voice.SpeechClient,"download",AsyncMock(side_effect=[TimeoutError(),wav()])) as download:
            worker.enqueue(self.now,manual=True)
            job=await self.drive(worker)
            self.assertEqual(job["voiceover"]["clips"][0]["phase"],"downloading")
            self.assertNotIn("private=signature",json.dumps(worker.state()))
            worker.resume(job["id"])
            self.assertEqual((await self.drive(worker))["phase"],"published")
        self.synthesize.assert_awaited_once();self.assertEqual(download.await_count,2)

    async def test_confirmed_rejection_can_resume_with_explicit_replacement_key(self):
        worker=self.worker();self.pipeline()
        self.synthesize.side_effect=[voice.SpeechRejected("no permission"),{"content":wav()}]
        worker.enqueue(self.now,manual=True)
        job=await self.drive(worker)
        self.service.save({"api_key":"replacement-speech-key"})
        worker.resume(job["id"])
        self.assertEqual((await self.drive(worker))["phase"],"published")
        self.assertEqual(self.synthesize.await_count,2)
        self.create.assert_awaited_once()

    async def test_cleanup_includes_raw_audio_but_preserves_changed_artifacts(self):
        worker=self.worker();self.pipeline()
        worker.enqueue(self.now,manual=True)
        job=await self.drive(worker)
        source=self.service.path(job,"-source.mp4")
        source.write_bytes(b"user edit")
        worker.verify_publication=AsyncMock(return_value=True)
        await worker.tick(self.now)
        self.assertEqual(worker.jobs()[0]["cleanup_state"],"kept")
        self.assertTrue(self.service.path(job,".mp4").exists())
        self.assertTrue(self.service.path(job,"-voice-01.wav").exists())

    async def test_cleanup_removes_only_verified_job_voice_artifacts(self):
        worker=self.worker();self.pipeline()
        untouched=self.path/"media"/"manual.mp4";untouched.write_bytes(b"user video")
        worker.enqueue(self.now,manual=True)
        job=await self.drive(worker)
        worker.verify_publication=AsyncMock(return_value=True)
        await worker.tick(self.now)
        self.assertEqual(worker.jobs()[0]["cleanup_state"],"deleted")
        self.assertFalse(self.service.path(job,"-source.mp4").exists())
        self.assertFalse(self.service.path(job,"-voice-01.wav").exists())
        self.assertEqual(untouched.read_bytes(),b"user video")

    async def test_script_validation_and_download_hosts(self):
        for text in ("{}",'{"segments":[]}','{"segments":[{"text":"too long for a one second clip"}]}'):
            with self.assertRaises(voice.VoiceoverError):voice.parse_narration(text,[1])
        self.assertNotIn("{",voice.parse_narration('{"segments":[{"text":"{\\\\a}"}]}',[6])[0]["text"])
        for url in ("http://127.0.0.1/key","https://evil.example/audio","https://aliyuncs.com.evil.example/x","https://u:p@bucket.aliyuncs.com/x"):
            with self.assertRaises(voice.VoiceoverError):voice.SpeechClient.download_url(url)
        self.assertTrue(voice.SpeechClient.download_url("http://bucket.aliyuncs.com/a.wav").startswith("https://"))

    async def test_speech_provider_http_payloads_status_and_anonymous_download(self):
        class Response:
            status=200
            def __init__(self,content):self.content=self;self.raw=content
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def raise_for_status(self):pass
            async def iter_chunked(self,size):
                for index in range(0,len(self.raw),333):yield self.raw[index:index+333]
        class Session:
            def __init__(self,*args,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def post(inner,url,**kwargs):
                calls.append((url,kwargs));return Response(json.dumps(response).encode())
            def get(inner,url,**kwargs):
                calls.append((url,kwargs));return Response(wav())
        calls=[]
        self.configure()
        config=self.service.pin()
        client=voice.SpeechClient(config,"not-printed-key")
        response={"base_resp":{"status_code":0},"data":{"audio":wav().hex()}}
        with patch.object(voice.aiohttp,"ClientSession",Session):
            result=await client.synthesize("开饭啦。")
            self.assertEqual(result["content"],wav())
            self.assertEqual(calls[-1][1]["json"]["voice_setting"]["voice_id"],config["voice"])
            self.assertFalse(calls[-1][1]["allow_redirects"])
            response={"base_resp":{"status_code":1004},"data":None}
            with self.assertRaises(voice.SpeechRejected):await client.synthesize("开饭啦。")
            self.service.save({"provider":"qwen","api_key":"qwen-key","voice":"Cherry","model":"qwen3-tts-flash"})
            client=voice.SpeechClient(self.service.pin(),"qwen-key")
            response={"output":{"audio":{"url":"http://bucket.aliyuncs.com/test.wav"}},"status_code":200}
            result=await client.synthesize("开饭啦。")
            self.assertEqual(calls[-1][1]["json"]["input"]["voice"],"Cherry")
            self.assertTrue(result["url"].startswith("https://"))
            self.assertEqual(await client.download(result["url"]),wav())
            self.assertNotIn("headers",calls[-1][1])
            Response.status=404
            with self.assertRaisesRegex(voice.VoiceoverError,"已过期"):
                await client.download(result["url"])

    async def test_speech_profile_is_pinned_and_preflight_failure_prevents_video_fee(self):
        worker=self.worker();self.pipeline()
        self.service.preflight.side_effect=voice.VoiceoverError("Missing subtitle font")
        worker.enqueue(self.now,manual=True)
        await worker.tick(self.now)
        self.create.assert_not_awaited()
        self.assertEqual(worker.jobs()[0]["phase"],"queued")
        self.assertIn("Missing subtitle font",worker.jobs()[0]["message"])
        original=worker.jobs()[0]["voiceover"]["voice"]
        self.service.save({"voice":"Chinese (Mandarin)_Warm_Girl"})
        self.assertEqual(worker.jobs()[0]["voiceover"]["voice"],original)

    async def test_disabled_voiceover_keeps_existing_video_pipeline_unchanged(self):
        worker=self.worker();self.pipeline()
        self.service.save({"enabled":False})
        worker.enqueue(self.now,manual=True)
        self.assertNotIn("voiceover",worker.jobs()[0])
        self.assertEqual((await self.drive(worker))["phase"],"published")
        self.synthesize.assert_not_awaited();self.render.assert_not_awaited()

    async def test_real_ffmpeg_mix_and_chinese_subtitles(self):
        self.configure(mix=True,font_size=36)
        job={"id":"a"*32,"voiceover":self.service.pin()}
        final=self.service.path(job,".mp4")
        speech=self.service.path(job,"-voice-01.wav")
        await voice.run_media("ffmpeg","-v","error","-y","-f","lavfi","-i","color=c=0x34556d:s=640x360:r=25:d=2","-f","lavfi","-i","sine=frequency=220:duration=2","-c:v","libx264","-threads","2","-pix_fmt","yuv420p","-c:a","aac","-shortest",str(final))
        await voice.run_media("ffmpeg","-v","error","-y","-f","lavfi","-i","sine=frequency=660:duration=0.8","-c:a","pcm_s16le",str(speech))
        job["cleanup_identity"]=voice.identity(final)
        source=self.service.path(job,"-source.mp4");os.link(final,source)
        config=job["voiceover"]
        config.update(source_identity=voice.identity(source),artifacts=[{"suffix":"-source.mp4","identity":voice.identity(source)},{"suffix":"-voice-01.wav","identity":voice.identity(speech)}],
                      clips=[{"phase":"done","text":"开饭啦。","window":2,"duration":await voice.audio_duration(speech),"identity":voice.identity(speech)}])
        before=await voice.run_media("ffmpeg","-v","error","-ss","0.4","-i",str(source),"-frames:v","1","-f","image2pipe","-vcodec","png","-")
        result=await self.service.render(job,100*1024*1024)
        metadata=await voice.probe_video(result)
        self.assertTrue(metadata["audio"])
        self.assertAlmostEqual(metadata["duration"],2,delta=.1)
        after=await voice.run_media("ffmpeg","-v","error","-ss","0.4","-i",str(result),"-frames:v","1","-f","image2pipe","-vcodec","png","-")
        from PIL import Image,ImageChops
        difference=ImageChops.difference(Image.open(io.BytesIO(before)).convert("RGB"),Image.open(io.BytesIO(after)).convert("RGB"))
        self.assertIsNotNone(difference.crop((100,280,540,355)).getbbox())
        self.assertIn("开饭啦",self.service.path(job,"-subtitles.srt").read_text())
        self.assertTrue(source.exists())
        # A restart after the atomic output swap reuses that output instead of overwriting it.
        same=await self.service.render(job,100*1024*1024)
        self.assertEqual(voice.identity(same),config["rendered_identity"])
