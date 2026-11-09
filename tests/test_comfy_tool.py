"""管理员在聊天里让本机 ComfyUI 生成短片：闸门、排队、成片回到原会话。"""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from test_appearance import plugin

OWNER = "10001"
SOURCE = "aiocqhttp:FriendMessage:10001"

SETTINGS = {
    "provider": "openai",
    "region": "custom",
    "profiles": {
        "openai:custom": {
            "key": "bridge-key",
            "model": "comfyui-h3",
            "base_url": "http://172.18.0.1:18188/v1",
            "transport": "multipart",
        }
    },
}


class FakeVideo:
    """替掉 CompatibleVideo：不碰网络，行为可按用例调（错误开关放类上，setUp 里重置）。"""

    instances: list["FakeVideo"] = []
    models_error = None
    create_error = None
    query_result = "task-abc123456789"
    query_error = None
    download_error = None

    def __init__(self, provider, region, key, model, workspace="", duration=None, *,
                 base_url="", transport="multipart"):
        self.provider = provider
        self.region = region
        self.key = key
        self.model = model
        self.duration = duration
        self.base_url = base_url
        self.transport = transport
        self.created: list[str] = []
        self.downloaded: list[Path] = []
        FakeVideo.instances.append(self)

    async def models(self):
        if self.models_error:
            raise self.models_error
        return [{"id": "comfyui-h3"}]

    async def create(self, prompt, *, reference_image=None):
        if self.create_error:
            raise self.create_error
        self.created.append(prompt)
        self.reference_image = reference_image
        return "task-abc123456789"

    async def query(self, task_id):
        if self.query_error:
            raise self.query_error
        return self.query_result

    async def download(self, task_id, temporary, destination):
        if self.download_error:
            raise self.download_error
        destination.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 64)
        self.downloaded.append(destination)


class ComfyToolTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = Path(directory.name)
        FakeVideo.instances = []
        FakeVideo.models_error = None
        FakeVideo.create_error = None
        FakeVideo.query_result = "task-abc123456789"
        FakeVideo.query_error = None
        FakeVideo.download_error = None
        self.patch = patch.object(plugin, "CompatibleVideo", FakeVideo)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.instance = plugin.BiliScrollPlugin.__new__(plugin.BiliScrollPlugin)
        self.instance.cfg = {}
        self.instance.data_dir = self.data_dir
        self.instance.context = SimpleNamespace(
            get_config=lambda: {"admins_id": [OWNER]},
            send_message=AsyncMock(),
        )
        self.instance._auto_publish = SimpleNamespace(
            settings=lambda: json.loads(json.dumps(SETTINGS))
        )
        self.instance._comfy_job = None
        self.instance._comfy_task = None
        self.instance.COMFY_POLL_SECONDS = 0
        self.instance.COMFY_TIMEOUT_SECONDS = 20

    def event(self, sender=OWNER):
        return SimpleNamespace(
            get_sender_id=lambda: sender,
            unified_msg_origin=SOURCE,
        )

    async def run_tool(self, **kwargs):
        text = await self.instance.tool_comfyui_video(self.event(), **kwargs)
        task = self.instance._comfy_task
        if task is not None:
            await task
        return text

    def sent_chains(self):
        return [call.args[1] for call in self.instance.context.send_message.await_args_list]

    async def test_owner_gets_video_back_in_the_same_chat(self):
        text = await self.run_tool(prompt="一只在雨里走的橘猫，跟拍，暖色路灯")
        self.assertIn("开始生成了", text)
        client = FakeVideo.instances[-1]
        self.assertEqual(client.duration, 4)
        self.assertEqual(client.base_url, "http://172.18.0.1:18188/v1")
        self.assertEqual(client.created, ["一只在雨里走的橘猫，跟拍，暖色路灯"])
        chains = self.sent_chains()
        self.assertEqual(len(chains), 1)
        component = chains[0].chain[0]
        self.assertEqual(type(component).__name__, "Video")
        self.assertTrue(str(component.file).endswith(".mp4"))
        self.assertTrue(Path(component.file).exists())
        self.assertIsNone(self.instance._comfy_job)

    async def test_non_owner_is_refused_without_any_request(self):
        text = await self.instance.tool_comfyui_video(self.event("999"), prompt="随便")
        self.assertIn("只有管理员", text)
        self.assertEqual(FakeVideo.instances, [])
        self.instance.context.send_message.assert_not_awaited()

    async def test_provider_must_be_the_compatible_service(self):
        self.instance._auto_publish = SimpleNamespace(
            settings=lambda: {"provider": "minimax", "region": "cn", "profiles": {}}
        )
        text = await self.run_tool(prompt="测试")
        self.assertIn("兼容", text)
        self.assertEqual(FakeVideo.instances, [])

    async def test_missing_key_or_address_is_refused(self):
        self.instance._auto_publish = SimpleNamespace(
            settings=lambda: json.loads(json.dumps({**SETTINGS, "profiles": {}}))
        )
        text = await self.run_tool(prompt="测试")
        self.assertIn("地址 / Key / 模型", text)
        self.assertEqual(FakeVideo.instances, [])

    async def test_bridge_down_gives_actionable_message(self):
        FakeVideo.models_error = OSError("connection refused")
        text = await self.run_tool(prompt="测试")
        self.assertIn("连不上本机 ComfyUI 桥接", text)
        self.assertIsNone(self.instance._comfy_job)

    async def test_seconds_are_snapped_to_supported_sizes(self):
        await self.run_tool(prompt="甲", seconds=10)
        self.assertEqual(FakeVideo.instances[-1].duration, 8)
        await self.run_tool(prompt="乙", seconds=12)
        self.assertEqual(FakeVideo.instances[-1].duration, 12)
        await self.run_tool(prompt="丙", seconds="abc")
        self.assertEqual(FakeVideo.instances[-1].duration, 4)

    async def test_second_request_waits_for_the_running_one(self):
        self.instance._comfy_job = {"task_id": "t", "seconds": 4, "at": "12:00", "session": SOURCE}
        text = await self.run_tool(prompt="再来一段")
        self.assertIn("还有一段在做", text)
        self.assertEqual(FakeVideo.instances, [])

    async def test_generation_failure_reports_in_chat(self):
        FakeVideo.query_error = plugin.GenerationFailed("兼容视频服务确认生成失败。")
        text = await self.run_tool(prompt="测试")
        self.assertIn("开始生成了", text)
        chains = self.sent_chains()
        self.assertEqual(len(chains), 1)
        plain = chains[0].chain[0]
        self.assertEqual(type(plain).__name__, "Plain")
        self.assertIn("没生成出来", plain.text)

    async def test_timeout_reports_in_chat(self):
        FakeVideo.query_result = None
        self.instance.COMFY_TIMEOUT_SECONDS = 0.05
        self.instance.COMFY_POLL_SECONDS = 0.01
        await self.run_tool(prompt="测试")
        plain = self.sent_chains()[0].chain[0]
        self.assertIn("还没出片", plain.text)

    async def test_empty_or_overlong_prompt_is_refused(self):
        self.assertIn("描述是空的", await self.run_tool(prompt="   "))
        self.assertIn("太长了", await self.run_tool(prompt="字" * 700))
        self.assertEqual(FakeVideo.instances, [])

    def use_reference_library(self, data_url=None, error=None):
        def choose(jobs):
            if error:
                raise error
            return {"name": "大肥鱼三维图.png"}

        def load(expected, *, provider):
            return {"name": expected["name"], "data_url": data_url or "data:image/png;base64,AAAA"}

        self.instance._auto_publish.jobs = lambda: []
        self.instance._auto_publish.references = SimpleNamespace(choose=choose, load=load)

    async def test_reference_image_is_passed_when_asked(self):
        self.use_reference_library()
        text = await self.run_tool(prompt="大肥鱼吃饭", reference=True)
        self.assertIn("带三视图参考", text)
        self.assertEqual(
            FakeVideo.instances[-1].reference_image, "data:image/png;base64,AAAA"
        )

    async def test_text_only_request_sends_no_reference(self):
        await self.run_tool(prompt="一只猫")
        self.assertIsNone(FakeVideo.instances[-1].reference_image)

    async def test_missing_reference_library_is_refused_before_submit(self):
        self.use_reference_library(error=plugin.ReferenceError("reference_images 里没有图"))
        text = await self.run_tool(prompt="大肥鱼吃饭", reference=True)
        self.assertIn("参考图用不了", text)
        self.assertEqual(FakeVideo.instances, [])

    async def test_old_clips_are_pruned(self):
        media = self.data_dir / "media"
        media.mkdir(parents=True, exist_ok=True)
        for index in range(8):
            (media / f"comfy-old{index}.mp4").write_bytes(b"x")
        self.instance._prune_comfy_files()
        self.assertEqual(len(list(media.glob("comfy-*.mp4"))), self.instance.COMFY_KEEP_FILES)

    async def test_tool_docstring_marks_it_as_admin_only(self):
        doc = self.instance.tool_comfyui_video.__doc__
        self.assertIn("只有管理员", doc)
        self.assertIn("prompt(string)", doc)
        self.assertIn("seconds(number)", doc)


if __name__ == "__main__":
    unittest.main()
