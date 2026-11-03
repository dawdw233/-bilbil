import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../pages/panel/index.html", import.meta.url), "utf8");
const script = html.match(/<script type="module" id="appearance-script">([\s\S]*?)<\/script>/)[1];
const helpers = script.slice(0, script.indexOf("const artEl ="));
function load(overrides = {}) {
  const sandbox = { console, ...overrides };
  vm.runInNewContext(helpers + "; globalThis.api = {normalizeAppearance, appearanceStorage, prepareIllustration};", sandbox);
  return sandbox.api;
}
const plain = (value) => JSON.parse(JSON.stringify(value));

test("外观参数保留有效的 0、限制范围并拒绝无效颜色", () => {
  const { normalizeAppearance } = load();
  const settings = plain(normalizeAppearance({ x: -5, y: 110, zoom: 999, opacity: 0, strength: 0, fit: "contain", mask: "#ABCDEF" }));
  assert.deepEqual(settings, { x: 0, y: 100, zoom: 250, opacity: 0, strength: 0, glass_enabled: true, glass: 50, fit: "contain", mask: "#abcdef" });
  const fallback = plain(normalizeAppearance({ zoom: "bad", opacity: "", mask: "url(https://example.invalid)", fit: "invalid" }));
  assert.equal(fallback.zoom, 100);
  assert.equal(fallback.opacity, 85);
  assert.equal(fallback.fit, "cover");
  assert.equal(fallback.mask, "#edf6fc");
  assert.equal(normalizeAppearance({glass_enabled:false,glass:0}).glass_enabled,false);
  assert.equal(normalizeAppearance({glass_enabled:false,glass:0}).glass,0);
  assert.equal(normalizeAppearance({glass:150}).glass,100);
});

test("不支持的格式和超大文件在解码之前被拒绝", async () => {
  let decodes = 0;
  const api = load({ createImageBitmap: () => { decodes++; } });
  await assert.rejects(api.prepareIllustration({ type: "image/svg+xml", size: 100 }), /PNG/);
  await assert.rejects(api.prepareIllustration({ type: "image/png", size: 21 * 1024 * 1024 }), /20 MB/);
  assert.equal(decodes, 0);
});

function storageFixture() {
  const state = {};
  let finish;
  let fail;
  const response = new Promise((resolve, reject) => { finish = resolve; fail = reject; });
  const api = load({window:{AstrBotPluginView:{
    ready: async () => {},
    apiGet: async path => {state.readPath = path; return {settings:{zoom:130},image:"data:image/png;base64,test"};},
    apiPost: async (path, body) => {state.writePath=path; state.body=body; return response;},
  }}});
  return {api,state,finish,fail};
}
test("通过插件接口保存图片与参数，收到确认后才报告成功", async () => {
  const {api,state,finish}=storageFixture();
  const record={settings:{x:0},image:"data:image/png;base64,test"};
  let resolved=false;
  const pending=api.appearanceStorage("readwrite",record).then(value=>{resolved=true;return value;});
  await new Promise(setImmediate);
  assert.equal(state.writePath,"appearance/save");
  assert.equal(state.body,record);
  assert.equal(resolved,false);
  finish({saved:true});
  assert.equal((await pending).saved,true);
});
test("刷新时从插件读取外观，失败或缺少保存确认时返回错误", async () => {
  const fixture=storageFixture();
  assert.equal((await fixture.api.appearanceStorage("readonly")).settings.zoom,130);
  assert.equal(fixture.state.readPath,"appearance/state");
  await fixture.api.appearanceStorage("default");
  assert.equal(fixture.state.readPath,"appearance/default");
  const failed=storageFixture();
  const rejection=assert.rejects(failed.api.appearanceStorage("readwrite",{}),/Service unavailable/);
  await new Promise(setImmediate);
  failed.fail(new Error("Service unavailable"));
  await rejection;
  const missing=storageFixture();
  const noConfirmation=assert.rejects(missing.api.appearanceStorage("readwrite",{}),/保存未完成/);
  await new Promise(setImmediate);
  missing.finish(undefined);
  await noConfirmation;
});
test("页面保留主要业务入口，插画设置具备独立的可访问标签", () => {
  const markup = html.replace(/<script\b[^>]*>[\s\S]*?<\/script>/g, "");
  const ids = [...markup.matchAll(/\sid="([^"]+)"/g)].map(match => match[1]);
  assert.equal(ids.length, new Set(ids).size, "页面元素 ID 不得重复");
  for (const id of ["btn-run", "btn-login", "tab-main", "tab-rhythm", "tab-upload", "tab-delivery", "tab-comments", "tab-reply", "f-speaking_style_bind_persona", "appearance-dialog", "art-file", "art-save", "art-cancel"]) assert.ok(ids.includes(id), id);
  assert.match(html, /<dialog[^>]*aria-labelledby="appearance-title"/);
  assert.match(html, /prefers-reduced-motion/);
  assert.equal((html.match(/<\/body>/g) || []).length, 1);
});
