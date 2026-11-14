import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../pages/panel/index.html", import.meta.url), "utf8");

const fill = html.slice(html.indexOf("function fillReplyForm(s)"), html.indexOf("async function loadReply()"));

function formContext() {
  const nodes = Object.fromEntries(
    ["f-reply_mode", "f-reply_check_minutes", "f-reply_max_per_day", "f-reply_interval_seconds", "f-reply_once_per_person"]
      .map(id => [id, {}]),
  );
  const context = { $: id => nodes[id] };
  vm.runInNewContext(fill + ";globalThis.fill = fillReplyForm;", context);
  return {nodes, fill: context.fill};
}

test("每天自动回复上限允许填 -1（不限），提示文案同步", () => {
  assert.match(html, /id="f-reply_max_per_day" type="number" min="-1"/);
  const label = html.slice(html.indexOf("每天最多自动回几条"), html.indexOf("两条回复间隔（秒）"));
  assert.match(label, /-1 表示不限/);
});

test("回填表单时 -1 与 0 原样带出", () => {
  for (const input of [-1, 0, 8]) {
    const {nodes, fill: fillForm} = formContext();
    fillForm({
      mode: "auto", check_minutes: 5, max_per_day: input, interval_seconds: 60, once_per_person: false,
    });
    assert.equal(nodes["f-reply_max_per_day"].value, input);
    assert.equal(nodes["f-reply_mode"].value, "auto");
    assert.equal(nodes["f-reply_check_minutes"].value, 5);
    assert.equal(nodes["f-reply_interval_seconds"].value, 60);
    assert.equal(nodes["f-reply_once_per_person"].checked, false);
  }
});

test("缺省值回落到默认，模式缺失时按草稿展示", () => {
  const {nodes, fill: fillForm} = formContext();
  fillForm({});
  assert.equal(nodes["f-reply_max_per_day"].value, 3);
  assert.equal(nodes["f-reply_check_minutes"].value, 10);
  assert.equal(nodes["f-reply_interval_seconds"].value, 90);
  assert.equal(nodes["f-reply_mode"].value, "draft");
  assert.equal(nodes["f-reply_once_per_person"].checked, true);
});
