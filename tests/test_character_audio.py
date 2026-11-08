"""Native dialogue and actual-audio subtitles; all cloud requests are mocked."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from importlib import import_module
from unittest.mock import AsyncMock, patch

from test_appearance import plugin
from test_auto_publish import PLAN
from test_story_director import directed_plan

auto = import_module(plugin.__package__+".auto_publish")
voice = import_module(plugin.__package__+".voiceover")
speech = import_module(plugin.__package__+".speech_recognition")
director = import_module(plugin.__package__+".story_director")


def speaking_plan(durations=None):
    data=json.loads(directed_plan(durations) if durations else PLAN)
    rows=data["shots"] if durations else [data]
    for row in rows:
        row.update(dialogue=[{"start":1,"end":4,"speaker":"小机器人","text":"这里真好。"}],narration="",voice_start=0)
    return json.dumps(data,ensure_ascii=False)


class CharacterAudioTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);(self.root/"media").mkdir()
        self.plan=AsyncMock()
        self.service=voice.VoiceoverService(self.root,self.plan)
        self.service.save({"enabled":True,"asr_api_key":"independent-asr-key"})

    def job(self):
        job={"id":"d"*32,"phase":"voiceover","file":"ai-"+"d"*32+".mp4","voiceover":self.service.pin(),"prompt":"计划中没有这句识别文字"}
        path=self.service.path(job,".mp4");path.write_bytes(b"original video")
        job["cleanup_identity"]=voice.identity(path)
        return job

    def mock_media(self):
        for obj,name,value in ((self.service,"preflight",AsyncMock()),(self.service,"source_has_audio",AsyncMock(return_value=True)),
                               (voice,"probe_video",AsyncMock(return_value={"duration":6,"audio":True})),
                               (voice,"run_media",AsyncMock(return_value=b"\0\0"*3200))):
            item=patch.object(obj,name,value);item.start();self.addCleanup(item.stop)

    async def test_native_mode_needs_no_tts_key_and_state_never_leaks_asr_key(self):
        self.assertEqual(self.service.pin()["mode"],"character")
        self.assertNotIn("provider",self.service.pin())
        self.assertNotIn("independent-asr-key",json.dumps(self.service.state()))
        self.assertNotIn("independent-asr-key",json.dumps(self.service.pin()))
        self.service.save({"remove_asr_key":True})
        self.assertEqual(self.service.pin()["asr_revision"],"")
        for payload in ({"mode":"bad"},{"asr_workspace":"https://bad"},{"voice_style":[]},{"asr_api_key":"bad key"}):
            with self.assertRaises(voice.VoiceoverError):self.service.save(payload)

    async def test_subtitles_use_recognized_audio_not_planned_lines_and_render_retry_reuses_asr(self):
        self.mock_media();job=self.job()
        recognized=[{"start":1.2,"end":2.1,"text":"实际识别的话。"}]
        def persisted():
            if job["voiceover"].get("asr",{}).get("phase")=="requesting":self.assertTrue(job["voiceover"]["asr"]["task_id"])
        async def render(*args):
            self.assertEqual(job["voiceover"]["asr"]["segments"],recognized)
            if rendering.await_count==1:raise voice.VoiceoverError("模拟烧录失败")
            return self.service.path(job,".mp4")
        with patch.object(voice.ParaformerClient,"recognize",AsyncMock(return_value=recognized)) as asr, \
             patch.object(self.service,"render",AsyncMock(side_effect=render)) as rendering, \
             patch.object(voice.SpeechClient,"synthesize",AsyncMock()) as tts:
            await self.service.step(job,persisted,auto.MAX_VIDEO_BYTES)
            self.assertEqual(job["voiceover"]["asr"]["phase"],"done")
            await self.service.step(job,persisted,auto.MAX_VIDEO_BYTES)
            self.assertEqual(job["phase"],"failed")
            self.service.save({"remove_asr_key":True})
            self.service.resume(job)
            await self.service.step(job,persisted,auto.MAX_VIDEO_BYTES)
            self.assertEqual(job["phase"],"ready")
            asr.assert_awaited_once();tts.assert_not_awaited();self.plan.assert_not_awaited()

    async def test_no_speech_and_subtitles_off_preserve_source_without_tts_or_caption_render(self):
        self.mock_media()
        with patch.object(voice.ParaformerClient,"recognize",AsyncMock(return_value=[])) as asr, \
             patch.object(self.service,"render",AsyncMock()) as render, patch.object(voice.SpeechClient,"synthesize",AsyncMock()) as tts:
            job=self.job()
            await self.service.step(job,lambda:None,auto.MAX_VIDEO_BYTES)
            await self.service.step(job,lambda:None,auto.MAX_VIDEO_BYTES)
            self.assertEqual(job["voiceover"]["skipped"],"no_speech")
            self.assertEqual(job["phase"],"ready")
            second=self.job();second["voiceover"]["subtitles"]=False
            await self.service.step(second,lambda:None,auto.MAX_VIDEO_BYTES)
            self.assertEqual(second["voiceover"]["skipped"],"subtitles_off")
            asr.assert_awaited_once();render.assert_not_awaited();tts.assert_not_awaited()

    async def test_unknown_recognition_does_not_auto_resubmit_but_explicit_retry_may(self):
        self.mock_media();job=self.job()
        job["voiceover"]["asr"]={"phase":"requesting","task_id":"a"*32}
        with patch.object(voice.ParaformerClient,"recognize",AsyncMock(return_value=[])) as asr:
            await self.service.step(job,lambda:None,auto.MAX_VIDEO_BYTES)
            self.assertEqual((job["phase"],job["retry_stage"]),("failed","voice"))
            asr.assert_not_awaited()
            self.service.resume(job)
            await self.service.step(job,lambda:None,auto.MAX_VIDEO_BYTES)
            asr.assert_awaited_once()

    async def test_missing_or_changed_asr_key_blocks_recognition_without_regenerating_video(self):
        job=self.job()
        self.service.save({"asr_api_key":"new-asr-key"})
        with self.assertRaises(voice.VoiceoverError):self.service.asr_key(job["voiceover"])
        self.service.resume(job)
        self.assertEqual(self.service.asr_key(job["voiceover"]),"new-asr-key")

    async def test_single_and_storyboard_plans_send_dialogue_to_native_video_model(self):
        now=plugin.datetime(2026,11,8,21,tzinfo=plugin.BEIJING_TIME)
        for mode in ("single","storyboard"):
            worker=auto.AutoPublisher(self.root,AsyncMock(return_value=speaking_plan()),AsyncMock(),lambda:"42",lambda:False,
                                     voiceover=self.service,plan_storyboard=AsyncMock(side_effect=lambda d,r,ts,**kw:speaking_plan(ts)))
            worker.jobs_path.unlink(missing_ok=True)
            worker.save_settings({"enabled":True,"provider":"wan","api_key":"video-only-key","duration":5,"mode":mode},now)
            worker.enqueue(now,manual=True)
            with patch.object(auto.WanVideo,"create",AsyncMock(return_value="task")) as create,patch.object(self.service,"preflight",AsyncMock()):
                await worker.tick(now)
                if mode=="storyboard":await worker.tick(now)
                prompt=create.await_args.args[0]
                self.assertIn('"这里真好。"',prompt)
                self.assertIn("角色声画同步",prompt)
                self.assertNotIn("不生成旁白、对白",prompt)
                self.assertIn("character_audio",worker.jobs()[0])
                if mode=="single":self.assertIn("character_audio",worker.plan.await_args.kwargs)

    async def test_silent_hailuo_not_falsely_advertised_as_lipsynced_and_needs_no_asr_key(self):
        self.service.save({"remove_asr_key":True})
        now=plugin.datetime(2026,11,8,21,tzinfo=plugin.BEIJING_TIME)
        worker=auto.AutoPublisher(self.root,AsyncMock(return_value=PLAN),AsyncMock(),lambda:"42",lambda:False,voiceover=self.service)
        worker.save_settings({"enabled":True,"api_key":"video-only-key"},now)
        self.assertNotIn("character_audio",worker.enqueue(now,manual=True))
        self.assertEqual(worker.state()["providers"]["minimax"]["native_audio_prefixes"],[])

    async def test_native_video_does_not_start_without_required_subtitle_recognition_key(self):
        self.service.save({"remove_asr_key":True})
        now=plugin.datetime(2026,11,8,21,tzinfo=plugin.BEIJING_TIME)
        worker=auto.AutoPublisher(self.root,AsyncMock(),AsyncMock(),lambda:"42",lambda:False,voiceover=self.service)
        worker.save_settings({"enabled":True,"provider":"wan","api_key":"video-key"},now)
        with self.assertRaisesRegex(auto.PublishError,"语音识别 Key"):
            worker.enqueue(now,manual=True)
        self.assertEqual(worker.jobs(),[])
        self.service.save({"subtitles":False})
        self.assertTrue(worker.enqueue(now,manual=True)["character_audio"])

    async def test_completed_video_is_transcribed_before_upload_across_video_providers(self):
        now=plugin.datetime(2026,11,8,21,tzinfo=plugin.BEIJING_TIME)
        for provider in ("minimax","ark","wan"):
            self.mock_media()
            self.service.save({"mode":"original"})
            publish=AsyncMock(return_value={"success":True,"bvid":"BV1234567890"})
            worker=auto.AutoPublisher(self.root,AsyncMock(return_value=PLAN),publish,lambda:"42",lambda:False,voiceover=self.service)
            worker.jobs_path.unlink(missing_ok=True)
            worker.save_settings({"enabled":True,"provider":provider,"api_key":"video-key"},now)
            async def render(job,*args):
                self.assertEqual(job["voiceover"]["asr"]["segments"][0]["text"],"实际说的话")
                return self.service.path(job,".mp4")
            with patch.object(auto.VIDEO_CLIENTS[provider],"create",AsyncMock(return_value="video-task")), \
                 patch.object(auto.VIDEO_CLIENTS[provider],"query",AsyncMock(return_value="video-file")), \
                 patch.object(auto.VIDEO_CLIENTS[provider],"download",AsyncMock(side_effect=lambda _id,_tmp,path:path.write_bytes(b"video with audio"))), \
                 patch.object(voice.ParaformerClient,"recognize",AsyncMock(return_value=[{"start":1,"end":2,"text":"实际说的话"}])) as asr, \
                 patch.object(self.service,"render",AsyncMock(side_effect=render)),patch.object(voice.SpeechClient,"synthesize",AsyncMock()) as tts:
                worker.enqueue(now,manual=True)
                for _ in range(10):
                    await worker.tick(now)
                    if worker.jobs()[0]["phase"] in ("failed","review","published"):break
                self.assertEqual(worker.jobs()[0]["phase"],"published",provider)
                asr.assert_awaited_once();publish.assert_awaited_once();tts.assert_not_awaited()

    async def test_planner_supports_native_dialogue_without_tts_narration_conflict(self):
        instance=plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        instance._provider_id=lambda:"text"
        llm=AsyncMock(return_value=type("Result",(),{"completion_text":speaking_plan()})())
        instance.context=type("Context",(),{"llm_generate":llm})()
        await instance._plan_auto_video("温室",[],6,character_audio={"style":"自然中文"})
        self.assertIn("dialogue",llm.await_args.kwargs["system_prompt"])
        await instance._plan_auto_storyboard("温室",[],[10]*3,character_audio={"style":"自然中文"})
        self.assertIn("不做旁白",llm.await_args.kwargs["system_prompt"])
        self.assertNotIn("不要求人物张嘴",llm.await_args.kwargs["system_prompt"])

    async def test_real_ffmpeg_burns_asr_timestamps_and_keeps_original_audio_packets(self):
        job=self.job();source=self.service.path(job,".mp4")
        await voice.run_media("ffmpeg","-v","error","-y","-f","lavfi","-i","color=blue:s=640x360:d=3", "-f","lavfi","-i","sine=frequency=660:duration=3", "-c:v","libx264","-threads","2","-c:a","aac","-t","3",str(source))
        job["cleanup_identity"]=voice.identity(source)
        original=self.service.path(job,"-source.mp4");os.link(source,original)
        job["voiceover"].update(source_identity=voice.identity(original),artifacts=[],asr={"phase":"done","segments":[{"start":1.1,"end":1.8,"text":"来自实际音轨。"}]})
        result=await self.service.render(job,auto.MAX_VIDEO_BYTES)
        srt=self.service.path(job,"-subtitles.srt").read_text()
        self.assertIn("00:00:01,100 --> 00:00:01,800",srt)
        self.assertNotIn("计划",srt)
        async def audio_hash(path):return await voice.run_media("ffmpeg","-v","error","-i",str(path),"-map","0:a:0","-c:a","copy","-f","hash","-hash","sha256","-")
        self.assertEqual(await audio_hash(original),await audio_hash(result))


class RecognitionProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_ws_waits_for_started_and_uses_only_final_actual_text_and_timestamp(self):
        task_id="a"*32;queue=asyncio.Queue();events=[]
        class WS:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            async def send_json(self,data):
                action=data["header"]["action"];events.append(action)
                header={"task_id":task_id,"event":"task-started" if action=="run-task" else "task-finished"}
                if action=="finish-task":
                    await queue.put({"header":{"task_id":task_id,"event":"result-generated"},"payload":{"output":{"sentence":{"sentence_end":True,"begin_time":120,"end_time":None,"text":"真实台词","words":[{"end_time":450}]}}}})
                await queue.put({"header":header})
            async def send_bytes(self,content):
                events.append("audio")
                await queue.put({"header":{"task_id":task_id,"event":"result-generated"},"payload":{"output":{"sentence":{"sentence_end":False,"text":"不采用的中间稿"}}}})
            async def receive(self,**kwargs):return type("Message",(),{"type":voice.aiohttp.WSMsgType.TEXT,"data":json.dumps(await queue.get())})()
        class Session:
            def __init__(self,*args,**kwargs):pass
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            def ws_connect(self,url,**kwargs):
                self_test.assertTrue(url.startswith("wss://"));self_test.assertEqual(kwargs["headers"]["Authorization"],"Bearer asr-key")
                return WS()
        self_test=self
        with patch.object(speech.aiohttp,"ClientSession",Session),patch.object(speech.asyncio,"sleep",AsyncMock()):
            result=await speech.ParaformerClient("asr-key").recognize(b"\0\0"*8000,1,task_id)
        self.assertEqual(result,[{"start":.12,"end":.45,"text":"真实台词"}])
        self.assertEqual(events[0],"run-task");self.assertEqual(events[-1],"finish-task")
        self.assertIn("audio",events)

    async def test_timestamp_validation_ignores_partial_results_and_rejects_fake_ranges(self):
        self.assertIsNone(speech.final_sentence({"sentence_end":False,"text":"partial"},5))
        for row in ({"start":True,"end":2,"text":"a"},{"start":1,"end":7,"text":"a"},{"start":2,"end":1,"text":"a"}):
            with self.assertRaises(speech.RecognitionError):speech.validate_segments([row],5)
        result=speech.final_sentence({"sentence_end":True,"begin_time":0,"end_time":1000,"text":"{\\x}<tag>"},5)
        self.assertNotIn("{",result["text"]);self.assertNotIn("\\",result["text"])

    async def test_bad_dialogue_rejected_before_paid_video(self):
        for row in ({"start":0,"end":7,"speaker":"A","text":"你好"},{"start":True,"end":3,"speaker":"A","text":"你好"},
                    {"start":0,"end":1,"speaker":"A","text":"这句实在太长了"}):
            data=json.loads(PLAN);data["dialogue"]=[row]
            with self.assertRaises(auto.PublishError):auto.parse_plan(json.dumps(data),6)

    async def test_connection_errors_are_safe_and_not_reported_as_no_speech(self):
        with patch.object(speech.aiohttp,"ClientSession",side_effect=RuntimeError("Authorization: private-key")):
            with self.assertRaises(speech.RecognitionError) as caught:
                await speech.ParaformerClient("private-key").recognize(b"\0\0"*100,1,"a"*32)
            self.assertNotIn("private-key",str(caught.exception))
