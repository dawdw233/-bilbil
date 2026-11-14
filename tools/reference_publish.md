# 参考投稿：插画与章节视频

这条独立流程为稿件制作封面和章节卡。参考
[大肥鱼教你全自动做视频](https://www.bilibili.com/video/BV1ybnH6sEAz/) 的画面布局：
深蓝背景、左侧步骤编号与要点、右侧随内容变化的角色插画，底部留给字幕和章节进度。
封面分别渲染 16:9 片头和 4:3 投稿版。

## 运行

1. 电脑启动 ComfyUI 和 `tools/comfyui_bridge.py`，远程 AstrBot 继续通过现有私有桥接访问。
   确认视频兼容服务的自定义存档仍保存本机桥接地址与 Key。这个步骤不读取当前选择的云端服务。
2. 在拥有 AstrBot 配置、Pillow 和 FFmpeg 的环境中，用
   `python tools/reference_publish.py --work <本期目录> --plan --topic "主题与已知事实"` 写稿，
   确认稿件后加 `--audio` 生成配音。也可使用已经配好音的 `script.json`、`sentences.json` 和 `audio/full.wav`。
3. 用原有 ASR 流程生成 `asr_alignment.json` 后运行 `--video`。加 `--cards` 可只预览卡片，
   不需要先配音或对齐。可用 `--reference` 传角色设定图，`--font` 指定字体。
4. 工具读取当前鲸汐人格卡，让 AstrBot 配置中的模型按本期内容规划角色姿势、道具与每章用图。
   `--art-count` 可选 1～6 张（默认 4），同类章节可以复用图片。桥接用已安装的 H3 Ref2VA 和
   SelfLift 生成 PNG。出图串行进行，已生成的图与分镜保存在本期 `illustrations/`，后续渲染可复用。
5. `--video` 生成的成片是本期 `final.mp4`。确认后才单独运行 `--upload`，该入口提交仅自己可见的 B 站稿件。

参考投稿的文件与现有手动、自主投稿记录独立。当前入口是脚本，暂未加入 QQ 命令或面板。
本地桥接不可用时，生成阶段会报告失败并保留已完成的插画。
