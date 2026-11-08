"""Reference pictures are synthetic; all LLM/video/upload calls are mocked."""
import json
import base64
import tempfile
import unittest
from importlib import import_module
from pathlib import Path
from unittest.mock import AsyncMock, patch

from PIL import Image
from test_appearance import plugin
from test_auto_publish import PLAN

auto = import_module(plugin.__package__ + ".auto_publish")
refs = import_module(plugin.__package__ + ".reference_images")


class ReferenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.library = refs.ReferenceLibrary(self.path)
        self.library.ensure()

    def picture(self, name="fish.png", color="blue", size=(640, 480)):
        path = self.library.path / name
        Image.new("RGB", size, color).save(path)
        return path

    def worker(self, **settings):
        (self.path / "media").mkdir(exist_ok=True)
        self.now = plugin.datetime(2026, 11, 7, 19, tzinfo=plugin.BEIJING_TIME)
        self.plan = AsyncMock(return_value=PLAN)
        self.worker_instance = auto.AutoPublisher(self.path,self.plan,AsyncMock(),lambda:"42",lambda:False)
        self.worker_instance.save_settings({"enabled":True,"api_key":"fake-key","reference_enabled":True,**settings},self.now)
        return self.worker_instance

    async def test_static_formats_scan_rotation_and_new_files(self):
        self.picture("a.png"); self.picture("b.jpg"); self.picture("c.webp")
        (self.library.path / "model.obj").write_text("ignored")
        self.assertEqual(self.library.scan()["count"],3)
        first = self.library.choose([])
        self.assertEqual(first["name"],"a.png")
        self.assertEqual(self.library.choose([{"reference_image":first}])["name"],"b.jpg")
        self.assertEqual(self.library.choose([{"reference_image":{"name":"c.webp"}}])["name"],"a.png")
        self.picture("d.png")
        self.assertEqual(self.library.choose([{"reference_image":{"name":"c.webp"}}])["name"],"d.png")
        self.assertTrue(self.library.load(first,provider="minimax")["data_url"].startswith("data:image/png;base64,"))

    def upload_image(self, color="blue"):
        import io
        stream=io.BytesIO()
        Image.new("RGB",(640,480),color).save(stream,format="PNG")
        return "data:image/png;base64,"+base64.b64encode(stream.getvalue()).decode()

    async def test_local_upload_is_private_idempotent_and_preserves_pinned_original(self):
        first=self.library.upload("大肥鱼.png",self.upload_image())
        metadata=self.library.choose([])
        again=self.library.upload("大肥鱼.png",self.upload_image())
        changed=self.library.upload("大肥鱼.png",self.upload_image("red"))
        self.assertTrue(first["created"])
        self.assertFalse(again["created"])
        self.assertEqual(first["name"],again["name"])
        self.assertNotEqual(first["name"],changed["name"])
        self.assertEqual(self.library.scan()["count"],2)
        self.library.load(metadata,provider="minimax")
        self.assertFalse(list(self.library.path.glob("*.tmp")))

    async def test_local_upload_rejects_invalid_inputs_without_creating_files(self):
        image=self.upload_image()
        for name,content in (("../escape.png",image),("bad\\path.png",image),("model.obj",image),
                             ("a.png",None),("a.png","https://example.com/pic.png"),
                             ("a.png","data:image/png;base64,%%%%"),("a.png","data:image/png;base64,"),
                             ("a.jpg",image.replace("image/png","image/jpeg")),
                             ("a.png","data:image/png;base64,"+"A"*(14*1024*1024))):
            with self.subTest(name=name),self.assertRaises(refs.ReferenceError):
                self.library.upload(name,content)
        self.assertEqual(list(self.library.path.iterdir()),[])

    async def test_local_upload_failure_leaves_no_partial_image_or_temporary_file(self):
        with patch.object(refs.os,"link",side_effect=PermissionError()):
            with self.assertRaisesRegex(refs.ReferenceError,"保存失败"):
                self.library.upload("fish.png",self.upload_image())
        self.assertEqual(list(self.library.path.iterdir()),[])

    async def test_upload_route_adds_to_library_without_changing_settings_or_starting_video(self):
        worker=self.worker(reference_enabled=False)
        before=worker.settings_path.read_bytes()
        instance=plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        instance._auto_publish=worker
        payload={"name":"fish.png","image":self.upload_image()}
        class Request:
            async def json(self,default=None):return payload
        with patch.object(plugin,"request",Request()),patch.object(plugin,"json_response",lambda value:value),patch.object(plugin,"error_response",lambda message:{"error":message}):
            result=await instance._api_auto_reference_upload()
            self.assertTrue(result["uploaded"])
            self.assertEqual(result["references"]["count"],1)
            payload["name"]="../escape.png"
            self.assertIn("error",await instance._api_auto_reference_upload())
        self.assertEqual(worker.settings_path.read_bytes(),before)
        self.assertEqual(worker.jobs(),[])
        self.plan.assert_not_awaited()

    async def test_invalid_size_shape_corruption_and_path_are_rejected(self):
        self.picture("small.png",size=(300,300))
        self.picture("wide.png",size=(1000,350))
        (self.library.path/"bad.jpg").write_bytes(b"not an image")
        (self.library.path/"big.png").write_bytes(b"x"*(refs.MAX_IMAGE_BYTES+1))
        state=self.library.scan()
        self.assertEqual((state["count"],state["invalid_count"]),(0,4))
        for name in ("../secret.png","sub/file.png","file\\bad.png","x\n.png"):
            with self.assertRaises(refs.ReferenceError):
                self.library.load({"name":name},provider="minimax")

    async def test_symlinks_and_directory_links_are_never_read(self):
        outside=self.path/"outside.png"
        Image.new("RGB",(640,480)).save(outside)
        (self.library.path/"link.png").symlink_to(outside)
        self.assertEqual(self.library.scan()["invalid_count"],1)
        (self.library.path/"link.png").unlink()
        self.library.path.rmdir()
        self.library.path.symlink_to(self.path,target_is_directory=True)
        self.assertTrue(self.library.scan()["error"])

    async def test_empty_library_does_not_enqueue_or_call_models(self):
        worker=self.worker()
        with self.assertRaisesRegex(auto.PublishError,"没有可用参考图"):
            worker.enqueue(self.now,manual=True)
        self.assertEqual(worker.jobs(),[])
        self.plan.assert_not_awaited()
        for value in (1,"true",None):
            with self.assertRaises(auto.PublishError):
                worker.save_settings({"reference_enabled":value},self.now)
        with self.assertRaises(auto.PublishError):
            worker.save_settings({"model":"T2V-01"},self.now)

    async def test_real_picture_bytes_reach_both_planning_and_video_adapter(self):
        source=self.picture()
        worker=self.worker()
        worker.enqueue(self.now,manual=True)
        with patch.object(auto.MiniMaxVideo,"create",AsyncMock(return_value="task")) as create:
            await worker.tick(self.now)
        data_url=self.plan.await_args.kwargs["reference"]["data_url"]
        self.assertEqual(create.await_args.kwargs["reference_image"],data_url)
        self.assertEqual(worker.jobs()[0]["phase"],"generating")
        public=json.dumps(worker.state())
        self.assertNotIn("base64",public)
        self.assertNotIn("data:image",worker.jobs_path.read_text())
        self.assertNotIn("sha256",public)
        self.assertTrue(source.exists())
        self.assertEqual(worker.state()["jobs"][0]["reference_image"],"fish.png")

    async def test_picture_swapped_during_planning_stops_before_paid_request(self):
        self.picture()
        worker=self.worker()
        worker.enqueue(self.now,manual=True)
        async def swap(*args,**kwargs):
            self.picture(color="red")
            return PLAN
        self.plan.side_effect=swap
        with patch.object(auto.MiniMaxVideo,"create",AsyncMock()) as create:
            await worker.tick(self.now)
        self.assertEqual(worker.jobs()[0]["phase"],"failed")
        create.assert_not_awaited()

    async def test_missing_reference_or_vision_failure_cannot_fall_back_to_text(self):
        source=self.picture()
        worker=self.worker()
        worker.enqueue(self.now,manual=True)
        source.unlink()
        with patch.object(auto.MiniMaxVideo,"create",AsyncMock()) as create:
            await worker.tick(self.now)
        create.assert_not_awaited()
        self.plan.assert_not_awaited()
        self.assertEqual(worker.jobs()[0]["phase"],"failed")
        self.picture()
        worker.jobs_path.unlink()
        worker.enqueue(self.now,manual=True)
        self.plan.side_effect=ValueError("vision unsupported")
        with patch.object(auto.MiniMaxVideo,"create",AsyncMock()) as create:
            await worker.tick(self.now)
        create.assert_not_awaited()
        self.assertEqual(worker.jobs()[0]["phase"],"failed")

    async def test_provider_payloads_use_their_image_to_video_fields(self):
        for provider,model,expected in (("minimax","MiniMax-Hailuo-2.3","MiniMax-Hailuo-2.3"),
                                       ("ark","doubao-seedance-2-5-260628","doubao-seedance-2-5-260628"),
                                       ("wan","wan2.6-t2v","wan2.6-i2v")):
            client=auto.VIDEO_CLIENTS[provider](provider,"cn","fake-key",model)
            with patch.object(client,"_request",AsyncMock(return_value={"task_id":"task","id":"task","output":{"task_id":"task"}})) as request:
                await client.create("鱼吃饭",reference_image="data:image/png;base64,fixture")
            body=request.await_args.kwargs["json"]
            self.assertEqual(body["model"],expected)
            if provider=="minimax": self.assertEqual(body["first_frame_image"],"data:image/png;base64,fixture")
            elif provider=="ark": self.assertEqual(body["content"][1]["role"],"first_frame")
            else:
                self.assertEqual(body["input"]["img_url"],"data:image/png;base64,fixture")
                self.assertEqual(body["parameters"]["resolution"],"720P")
                self.assertNotIn("size",body["parameters"])
        worker=self.worker(provider="wan",model="wan2.5-t2v-preview")
        self.picture()
        self.assertEqual(worker.enqueue(self.now,manual=True)["model"],"wan2.5-i2v-preview")

    async def test_reference_data_url_is_accepted_by_installed_astrbot_and_alpha_is_checked(self):
        from astrbot.core.provider.entities import resolve_image_ref_to_base64_data
        self.picture()
        chosen=self.library.choose([])
        reference=self.library.load(chosen,provider="minimax")
        self.assertIsNotNone(await resolve_image_ref_to_base64_data(reference["data_url"],strict=True))
        Image.new("RGBA",(640,480),(0,0,255,100)).save(self.library.path/"fish.png")
        chosen=self.library.choose([])
        with self.assertRaisesRegex(refs.ReferenceError,"透明"):
            self.library.load(chosen,provider="wan")

    async def test_planning_callbacks_pass_reference_to_the_astrbot_vision_model(self):
        instance=plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        instance._provider_id=lambda:"vision-model"
        instance.context=type("Context",(),{"llm_generate":AsyncMock(return_value=type("Result",(),{"completion_text":"{}"})())})()
        reference={"data_url":"data:image/png;base64,fixture"}
        await instance._plan_auto_video("吃饭",[],6,reference=reference)
        await instance._plan_auto_storyboard("吃饭",[],[10,10,10],reference=reference)
        for call in instance.context.llm_generate.await_args_list:
            self.assertEqual(call.kwargs["image_urls"],[reference["data_url"]])
            self.assertIn("参考图",call.kwargs["system_prompt"])

    async def test_storyboard_uses_one_pinned_image_and_restores_before_retry(self):
        from test_storyboard import plan
        source=self.picture()
        original=source.read_bytes()
        worker=self.worker(mode="storyboard",duration=10)
        worker.plan_storyboard=AsyncMock(return_value=plan([10,10,10]))
        worker.enqueue(self.now,manual=True)
        with patch.object(auto.MiniMaxVideo,"create",AsyncMock(return_value="task")) as create, \
             patch.object(auto.MiniMaxVideo,"query",AsyncMock(return_value="file")), \
             patch.object(auto.MiniMaxVideo,"download",AsyncMock(side_effect=lambda _id,_tmp,dest:dest.write_bytes(b"clip"))):
            await worker.tick(self.now)
            await worker.tick(self.now)
            await worker.tick(self.now)
            self.picture(color="red")
            await worker.tick(self.now)
            self.assertEqual(create.await_count,1)
            job=worker.jobs()[0]
            self.assertEqual((job["phase"],job["retry_stage"]),("failed","shot"))
            source.write_bytes(original)
            worker.resume(job["id"])
            await worker.tick(self.now)
            self.assertEqual(create.await_count,2)
            self.assertEqual(create.await_args_list[0].kwargs["reference_image"],create.await_args_list[1].kwargs["reference_image"])
        worker.plan_storyboard.assert_awaited_once()
