import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../pages/panel/index.html", import.meta.url), "utf8");
const sync = html.slice(html.indexOf("function syncCommentTasteForm()"), html.indexOf("function syncSpeakingStyleForm()"));
const save = html.slice(html.indexOf("async function saveComments("), html.indexOf("// 每个区块只保存自己的字段。"));

test("口味开关停用输入但保留内容，重新勾选恢复编辑", () => {
  const nodes = Object.fromEntries(["f-comment_taste_enabled", "f-comment_taste", "f-comment_eagerness", "c2t-mode-status", "c2t-save-hint"].map(id => [id, {}]));
  nodes["f-comment_taste"].value = "只喜欢小猫";
  const context = { $: id => nodes[id] };
  vm.runInNewContext(sync, context);
  nodes["f-comment_taste_enabled"].checked = false;
  nodes["f-comment_taste_enabled"].onchange();
  assert.equal(nodes["f-comment_taste"].disabled, true);
  assert.equal(nodes["f-comment_eagerness"].disabled, true);
  assert.equal(nodes["f-comment_taste"].value, "只喜欢小猫");
  nodes["f-comment_taste_enabled"].checked = true;
  nodes["f-comment_taste_enabled"].onchange();
  assert.equal(nodes["f-comment_taste"].disabled, false);
  assert.equal(nodes["f-comment_taste"].value, "只喜欢小猫");
});

test("口味区只保存开关与口味，失败时不报告已保存", async () => {
  const nodes = Object.fromEntries(["c2t-btn-save", "c2t-save-hint", "f-auto_comment_per_day", "f-comment_taste_enabled", "f-comment_taste"].map(id => [id, {}]));
  nodes["f-comment_taste_enabled"].checked = false;
  nodes["f-comment_taste"].value = "喜欢音乐";
  let payload, fail = false;
  const context = {$: id => nodes[id], c2ShowErr: () => {}, loadComments: async () => {},
    api: async (path, body) => { if (fail) throw Error("写入失败"); payload = body; }};
  vm.runInNewContext(save + ";globalThis.save = saveComments;", context);
  await context.save("c2t-btn-save", "c2t-save-hint");
  assert.deepEqual(JSON.parse(JSON.stringify(payload)), {comment_taste_enabled:false,comment_taste:"喜欢音乐"});
  assert.equal(nodes["c2t-save-hint"].textContent, "已保存");
  fail = true;
  await context.save("c2t-btn-save", "c2t-save-hint");
  assert.match(nodes["c2t-save-hint"].textContent, /保存失败/);
  assert.equal(nodes["c2t-btn-save"].disabled, false);
});
