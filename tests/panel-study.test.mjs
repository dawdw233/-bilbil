import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, statSync } from "node:fs";
import vm from "node:vm";
const html = readFileSync(new URL("../pages/panel/index.html", import.meta.url), "utf8");
const script = html.match(/<script type="module" id="study-script">([\s\S]*?)<\/script>/)[1];
const sandbox = {};
vm.runInNewContext(script.slice(0, script.indexOf("const studyEl")) + ";globalThis.api={studyCount,studyProgress,studyTimelineMarkup};", sandbox);
const api = sandbox.api;
test("圆环按真实计划计算，未设置计划不显示虚假的完成进度", () => {
  assert.equal(api.studyProgress({ completed: 3, target: 6 }).percent, 50);
  assert.equal(api.studyProgress({ completed: 9, target: 6 }).percent, 100);
  assert.equal(api.studyProgress({ completed: 0, target: 0 }).percent, 0);
  assert.equal(api.studyCount("bad"), 0);
  assert.equal(api.studyCount(-2), 0);
});
test("时间轴转义内容并限制状态类名，保留最新五个时刻", () => {
  const items = Array.from({ length: 6 }, (_, i) => ({ time: `${i}:00`, title: '<img src=x onerror="alert(1)">', detail: "<script>", kind: "invalid onclick=bad" }));
  const rendered = api.studyTimelineMarkup(items);
  assert.ok(!rendered.includes("0:00"));
  assert.ok(rendered.includes("5:00"));
  assert.ok(rendered.includes("&lt;img"));
  assert.ok(!rendered.includes("<img"));
  assert.ok(!rendered.includes("onclick=bad"));
  assert.match(api.studyTimelineMarkup([]), /还没有安排/);
});
test("欢迎卡、分组导航与默认插画作为项目资源提供", () => {
  for (const text of ["一隅书房", "nav-group-label\">阅读", "nav-group-label\">写作", "nav-group-label\">生活", "study-timeline", 'role="progressbar"']) assert.ok(html.includes(text), text);
  const asset = new URL("../pages/panel/assets/study-hero.png", import.meta.url);
  assert.ok(statSync(asset).size < 3 * 1024 * 1024);
  assert.equal(readFileSync(asset).subarray(1, 4).toString(), "PNG");
});
