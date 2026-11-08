"""Cloud calls are mocked; real local media tests check cut points and timing."""
import io
import json
import os
import shutil
import struct
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image
from test_appearance import plugin
from test_storyboard import plan

auto = import_module(plugin.__package__ + ".auto_publish")
director = import_module(plugin.__package__ + ".story_director")
voice = import_module(plugin.__package__ + ".voiceover")


def directed_plan(durations, subject="小机器人整理温室"):
    data = json.loads(plan(durations))
    data.update(title=subject, prompt=subject, continuity="同一主体、同一温室、午后柔光，保持道具方位。")
    for index, shot in enumerate(data["shots"]):
        shot.update(beat=f"推进第{index+1}步", start_state=f"状态{index}", end_state=f"状态{index+1}",
                    prompt=f"完成第{index+1}步，动作自然。", camera="平视中景，缓慢推进", sound="轻微脚步与工具声",
                    narration="准备好了。" if index % 2 == 0 else "", voice_start=1)
    return json.dumps(data, ensure_ascii=False)


class DirectorPlanningTests(unittest.TestCase):
    def test_general_plan_preserves_per_shot_state_timing_and_silence(self):
        for subject in ("小机器人整理温室", "雨后森林的水滴"):
            data = auto.parse_storyboard(directed_plan([10, 10, 10], subject), [10]*3)
            self.assertEqual(data["title"], subject)
            self.assertEqual(data["shots"][1]["narration"], "")
            clips = director.narration_clips(data, 30)
            self.assertEqual([clip["start"] for clip in clips], [1, 21])
            self.assertEqual([clip["window"] for clip in clips], [9, 9])
            self.assertIsNone(director.narration_clips(auto.parse_storyboard(plan([10]*3), [10]*3), 30))

    def test_invalid_or_partial_structured_scripts_are_rejected_before_video_fee(self):
        original = json.loads(directed_plan([10]*3))
        for field, value in (("voice_start", True), ("voice_start", float("nan")), ("voice_start", 10),
                             ("narration", "太长"*20), ("end_state", ""), ("sound", [])):
            data = json.loads(json.dumps(original))
            data["shots"][1][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(auto.PublishError):
                auto.parse_storyboard(json.dumps(data), [10]*3)
        original["shots"][1] = {"prompt": "普通镜头", "duration": 10}
        with self.assertRaises(auto.PublishError):
            auto.parse_storyboard(json.dumps(original), [10]*3)

    def test_sound_is_capability_gated_and_narration_not_sent_as_character_dialogue(self):
        plan_data = auto.parse_storyboard(directed_plan([10]*3), [10]*3)
        for provider, model, supported in (("minimax", "MiniMax-Hailuo-2.3", False), ("ark", "ep-private", False),
                                           ("ark", "doubao-seedance-2-5-test", True), ("wan", "wan2.6-i2v", True)):
            prompt = director.shot_prompt({**plan_data, "provider":provider, "model":model, "shot_link":"continue"}, 1)
            self.assertIn("上一镜实际结束画面", prompt)
            self.assertEqual("画面同步环境音" in prompt, supported)
            self.assertNotIn("准备好了。", prompt)
            if supported:
                self.assertIn("不生成旁白、对白", prompt)


class DirectorPipelineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name); (self.root/"media").mkdir()
        self.now = plugin.datetime(2026,11,8,20,tzinfo=plugin.BEIJING_TIME)
        self.publish = AsyncMock(return_value={"success": True, "bvid":"BV1234567890"})
        self.verify = AsyncMock(return_value=False)
        self.planner = AsyncMock(return_value=directed_plan([10]*3))
        self.compose = AsyncMock(side_effect=lambda root,jid,*args:self.write(root/"media"/f"ai-{jid}.mp4", b"composed"))
        self.create = AsyncMock(return_value="task-id")
        self.handoff = AsyncMock(side_effect=self.frame)
        for obj, name, value in ((auto.MiniMaxVideo,"create",self.create), (auto.MiniMaxVideo,"query",AsyncMock(return_value="file-id")),
                                 (auto.MiniMaxVideo,"download",AsyncMock(side_effect=lambda artifact,tmp,dest:self.write(dest,b"video"))),
                                 (auto,"handoff_frame",self.handoff)):
            item=patch.object(obj,name,value);item.start();self.addCleanup(item.stop)
        self.worker = self.make_worker()
        self.worker.save_settings({"enabled":True,"api_key":"test-key","mode":"storyboard","shot_link":"continue"},self.now)
        self.worker.enqueue(self.now,manual=True)

    def write(self,path,content):
        path.write_bytes(content);return path

    async def frame(self, root, job, index):
        source=root/"media"/job["shots"][index-1]["file"]
        if director.file_identity(source) != job["shots"][index-1]["identity"]:
            raise auto.CompositionError("上一镜素材已修改")
        path=director.frame_path(root,job["id"],index);path.parent.mkdir(exist_ok=True)
        path.write_bytes(b"frame")
        return {"record":{"identity":director.file_identity(path),"sha256":"hash"},"data_url":f"data:image/jpeg;base64,frame-{index}"}

    def make_worker(self):
        return auto.AutoPublisher(self.root,AsyncMock(),self.publish,lambda:"42",lambda:False,
                                  plan_storyboard=self.planner,compose=self.compose,verify_publication=self.verify)

    async def drive(self):
        for _ in range(30):
            await self.worker.tick(self.now)
            job=self.worker.jobs()[0]
            if job["phase"] in ("failed","review","published"):return job
        self.fail("Pipeline stalled")

    async def test_chain_survives_restart_uses_tail_frames_and_cleans_only_owned_frames(self):
        await self.worker.tick(self.now);await self.worker.tick(self.now);await self.worker.tick(self.now)
        self.worker=self.make_worker()
        job=await self.drive()
        self.assertEqual(job["phase"],"published")
        self.assertEqual(self.create.await_count,3)
        self.assertNotIn("reference_image",self.create.await_args_list[0].kwargs)
        self.assertEqual([call.kwargs["reference_image"] for call in self.create.await_args_list[1:]],
                         ["data:image/jpeg;base64,frame-1","data:image/jpeg;base64,frame-2"])
        self.planner.assert_awaited_once()
        state=json.dumps(self.worker.state(),ensure_ascii=False)
        self.assertIn("平视中景",state);self.assertNotIn("data:image",state);self.assertNotIn("test-key",state)
        unrelated=self.root/"story_frames"/"user.jpg";unrelated.write_bytes(b"untouched")
        self.verify.return_value=True
        await self.worker.tick(self.now)
        self.assertFalse(director.frame_path(self.root,job["id"],1).exists())
        self.assertTrue(unrelated.is_file())

    async def test_frame_failure_retry_does_not_regenerate_finished_video(self):
        real=self.handoff.side_effect
        async def flaky(*args):
            if self.handoff.await_count==1:raise auto.CompositionError("提取失败")
            return await real(*args)
        self.handoff.side_effect=flaky
        job=await self.drive()
        self.assertEqual((job["phase"],job["retry_stage"]),("failed","shot"))
        self.assertEqual(self.create.await_count,1)
        self.worker.resume(job["id"])
        self.assertEqual((await self.drive())["phase"],"published")
        self.assertEqual(self.create.await_count,3)

    async def test_switching_off_during_frame_extraction_does_not_submit_paid_request(self):
        real=self.handoff.side_effect
        async def disable(*args):
            result=await real(*args);self.worker.save_settings({"enabled":False},self.now);return result
        self.handoff.side_effect=disable
        for _ in range(4):await self.worker.tick(self.now)
        self.create.assert_awaited_once()
        self.assertEqual(self.worker.jobs()[0]["shots"][1]["phase"],"queued")

    async def test_modified_previous_clip_blocks_next_paid_request(self):
        for _ in range(3):await self.worker.tick(self.now)
        job=self.worker.jobs()[0]
        (self.root/"media"/job["shots"][0]["file"]).write_bytes(b"manually changed")
        job=await self.drive()
        self.assertEqual(job["phase"],"failed");self.create.assert_awaited_once()

    async def test_unsupported_model_rejected_before_enqueue_or_generation(self):
        with self.assertRaises(auto.PublishError):
            self.worker.save_settings({"model":"T2V-01"},self.now)
        self.create.assert_not_awaited()

    async def test_ark_audio_parameter_only_for_known_capable_models(self):
        for model, expected in (("doubao-seedance-2-5-test",True),("ep-private",False)):
            client=auto.ArkVideo("ark","cn","test-key",model)
            with patch.object(client,"_request",AsyncMock(return_value={"id":"task"})) as request:
                await client.create("环境音，无对白")
                self.assertEqual("generate_audio" in request.await_args.kwargs["json"],expected)

    async def test_voiceover_reuses_script_and_all_silent_plan_never_calls_tts(self):
        service=voice.VoiceoverService(self.root,AsyncMock())
        service.save({"enabled":True,"mode":"narration","api_key":"voice-key"})
        job={"id":"c"*32,"voiceover":service.pin(),**auto.parse_storyboard(directed_plan([10]*3),[10]*3)}
        final=service.path(job,".mp4");final.write_bytes(b"original")
        job["cleanup_identity"]=voice.identity(final)
        for shot in job["shots"]:shot["narration"]=""
        with patch.object(service,"preflight",AsyncMock()), patch.object(voice,"probe_video",AsyncMock(return_value={"duration":30})), \
             patch.object(service,"source_has_audio",AsyncMock(return_value=True)), \
             patch.object(service,"render",AsyncMock(return_value=final)), patch.object(voice.SpeechClient,"synthesize",AsyncMock()) as tts:
            await service.step(job,lambda:None,auto.MAX_VIDEO_BYTES)
            self.assertEqual(job["voiceover"]["clips"],[])
            self.assertTrue(job["voiceover"]["script_ready"])
            await service.step(job,lambda:None,auto.MAX_VIDEO_BYTES)
            self.assertEqual(job["phase"],"ready")
            service.plan.assert_not_awaited();tts.assert_not_awaited()


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "Requires FFmpeg")
class DirectorRealMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_frame_uses_trimmed_cut_point_reuses_file_and_rejects_modification(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"media").mkdir();jid="a"*32
            source=root/"media"/f"ai-{jid}-shot-01.mp4"
            await director.run_media("ffmpeg","-v","error","-y","-f","lavfi","-i","color=blue:s=640x360:d=2",
                                     "-f","lavfi","-i","color=red:s=640x360:d=2","-filter_complex","[0:v][1:v]concat=n=2:v=1:a=0[v]",
                                     "-map","[v]","-c:v","libx264","-threads","2",str(source))
            job={"id":jid,"shots":[{"phase":"done","duration":2,"file":source.name,"identity":director.file_identity(source)},{}]}
            frame=await director.handoff_frame(root,job,1);job["shots"][1]["handoff"]=frame["record"]
            path=director.frame_path(root,jid,1)
            with Image.open(path) as image:
                red,green,blue=image.getpixel((image.width//2,image.height//2))
                self.assertGreater(blue,red+150)
            self.assertEqual(frame["record"],(await director.handoff_frame(root,job,1))["record"])
            path.write_bytes(b"changed")
            with self.assertRaises(auto.CompositionError):await director.handoff_frame(root,job,1)

    async def test_render_delays_voice_and_subtitles_and_accepts_all_silent_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/"media").mkdir()
            service=voice.VoiceoverService(root,AsyncMock())
            service.save({"enabled":True,"mode":"narration","api_key":"test-key","subtitles":True})
            job={"id":"b"*32,"voiceover":service.pin()}
            final=service.path(job,".mp4")
            await voice.run_media("ffmpeg","-v","error","-y","-f","lavfi","-i","color=c=0x34556d:s=640x360:d=4","-c:v","libx264","-threads","2",str(final))
            source=service.path(job,"-source.mp4");os.link(final,source)
            sound=service.path(job,"-voice-01.wav")
            await voice.run_media("ffmpeg","-v","error","-y","-f","lavfi","-i","sine=frequency=660:duration=0.5","-c:a","pcm_s16le",str(sound))
            config=job["voiceover"];job["cleanup_identity"]=voice.identity(final)
            config.update(source_identity=voice.identity(source),artifacts=[],clips=[{"phase":"done","text":"你好。","start":2,"window":1,
                          "duration":await voice.audio_duration(sound),"identity":voice.identity(sound)}])
            result=await service.render(job,auto.MAX_VIDEO_BYTES)
            self.assertIn("00:00:02,000 --> 00:00:02,500",service.path(job,"-subtitles.srt").read_text())
            async def peak(at):
                raw=await voice.run_media("ffmpeg","-v","error","-ss",str(at),"-i",str(result),"-t","0.15","-f","s16le","-ac","1","-")
                return max(abs(x[0]) for x in struct.iter_unpack("<h",raw))
            self.assertLess(await peak(.5),10);self.assertGreater(await peak(2.1),100)
            # All-silent plans bypass paid synthesis and still render valid audio.
            config.pop("rendered_identity");config.update(clips=[],subtitles=False)
            job["cleanup_identity"]=voice.identity(final)
            result=await service.render(job,auto.MAX_VIDEO_BYTES)
            self.assertTrue((await voice.probe_video(result))["audio"])
