import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../pages/panel/index.html", import.meta.url), "utf8");

function sliceAfter(anchor, from, to) {
  const anchorAt = html.indexOf(anchor);
  assert.notEqual(anchorAt, -1, `找不到锚点 ${anchor}`);
  const start = html.indexOf(from, anchorAt);
  assert.notEqual(start, -1, `锚点 ${anchor} 之后找不到 ${from}`);
  const end = html.indexOf(to, start);
  assert.notEqual(end, -1, `找不到 ${from} 之后的分界 ${to}`);
  return html.slice(start, end);
}

test("手动投稿表单有 AI 声明、4:3 封面、定时、话题与仅自己可见", () => {
  for (const id of ["u-ai_declare", "u-cover43", "u-private", "u-schedule", "u-topic", "u-btn-review"]) {
    assert.match(html, new RegExp(`id="${id}"`), `缺少控件 ${id}`);
  }
  assert.match(html, /id="u-ai_declare" type="checkbox" checked/, "AI 声明应默认勾上");
  assert.match(html, /id="u-cover43" type="checkbox" checked/, "4:3 封面应默认勾上");
  assert.match(html, /id="u-private" type="checkbox"(?! checked)/, "仅自己可见默认不勾");
  assert.match(html, /<option value="0">自动预测（按标题，推荐）<\/option>/, "分区应能选自动预测");
  assert.match(html, /id="u-btn-review" class="hide"/, "审核状态按钮默认隐藏");
});

test("自动投稿的分区下拉不带「自动预测（0）」这一项", () => {
  assert.match(html, /\.filter\(\(option\) => option\.value !== "0"\)/, "复制到自主投稿时应滤掉 value=0");
  assert.doesNotMatch(html, /u-auto-tid"\)\.innerHTML = \$"u-tid"\)\.innerHTML;/, "不能整段照抄（会把自动预测带过去）");
});

function payloadFor(values) {
  const block = sliceAfter('$("u-btn-start").onclick', "const payload = {", "  try {");
  const nodes = {};
  const defaults = {
    "u-path": "", "u-file": "clip.mp4", "u-title": "标题", "u-tid": "0", "u-tags": "AI",
    "u-desc": "", "u-cover": "", "u-source": "", "u-dynamic": "", "u-topic": "",
    "u-schedule": "", "u-no_reprint": true, "u-ai_declare": true, "u-cover43": true,
    "u-private": false,
  };
  for (const [key, value] of Object.entries({...defaults, ...values})) {
    nodes[key] = typeof value === "boolean" ? {checked: value} : {value};
  }
  const context = {$: (id) => nodes[id]};
  vm.runInNewContext(`${block}\nresult = payload;`, context);
  return context.result;
}

test("投稿按钮把新字段一起提交，分区默认 0（自动预测）", () => {
  const payload = payloadFor({});
  assert.equal(payload.tid, 0);
  assert.equal(payload.ai_declare, true);
  assert.equal(payload.cover_43, true);
  assert.equal(payload.private, false);
  assert.equal(payload.schedule, "");
  assert.equal(payload.topic, "");
});

test("投稿按钮如实转发定时、话题与关闭的声明", () => {
  const payload = payloadFor({
    "u-tid": "21", "u-schedule": "2026-11-15 20:00", "u-topic": "AI 创作",
    "u-ai_declare": false, "u-cover43": false, "u-private": true,
  });
  assert.equal(payload.tid, 21);
  assert.equal(payload.schedule, "2026-11-15 20:00");
  assert.equal(payload.topic, "AI 创作");
  assert.equal(payload.ai_declare, false);
  assert.equal(payload.cover_43, false);
  assert.equal(payload.private, true);
});

test("查审核状态走 upload/review 并展示状态与驳回原因", async () => {
  const block = sliceAfter('$("u-btn-review").onclick', 'lastUploadBvid', '$("h-btn-save").onclick');
  const handlerBlock = sliceAfter('$("u-btn-review").onclick', 'async () => {', '$("h-btn-save").onclick');
  const nodes = {"u-hint": {textContent: ""}, "u-btn-review": {disabled: false}};
  let called = null;
  const context = {
    $: (id) => nodes[id],
    lastUploadBvid: "BV1xx411c7mD",
    api: async (route, payload) => {
      called = [route, payload];
      return {state: 2, state_desc: "未通过", reject_reason: "封面违规", private: false, ai_declare: true, scheduled_time: ""};
    },
  };
  vm.runInNewContext(`handler = ${handlerBlock};`, context);
  await context.handler();
  assert.ok(block.includes("upload/review"), "应调用 upload/review");
  assert.equal(called[0], "upload/review");
  assert.equal(called[1].bvid, "BV1xx411c7mD");
  assert.match(nodes["u-hint"].textContent, /未通过/);
  assert.match(nodes["u-hint"].textContent, /封面违规/);
  assert.equal(nodes["u-btn-review"].disabled, false);
});
