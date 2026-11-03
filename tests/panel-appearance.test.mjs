import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../pages/panel/index.html", import.meta.url), "utf8");
const script = html.match(/<script type="module" id="appearance-script">([\s\S]*?)<\/script>/)[1];
const helpers = script.slice(0, script.indexOf("const artEl ="));
const meteorScript = html.match(/<script type="module" id="meteor-script">([\s\S]*?)<\/script>/)[1];
const meteorHelpers = meteorScript.slice(0, meteorScript.indexOf("const meteorEl ="));
function load(overrides = {}) {
  const sandbox = { console, ...overrides };
  vm.runInNewContext(helpers + "; globalThis.api = {normalizeAppearance, appearanceStorage, prepareIllustration, sharpnessKernel};", sandbox);
  return sandbox.api;
}
function loadMeteors(overrides = {}) {
  const sandbox = {...overrides};
  vm.runInNewContext(meteorHelpers + "; globalThis.api = {normalizeMeteors, meteorStorage, meteorLayout, renderMeteors};", sandbox);
  return sandbox.api;
}
const plain = (value) => JSON.parse(JSON.stringify(value));

test("外观参数保留有效的 0、限制范围并拒绝无效颜色", () => {
  const { normalizeAppearance } = load();
  const settings = plain(normalizeAppearance({ x: -5, y: 110, zoom: 999, opacity: 0, strength: 0, fit: "contain", mask: "#ABCDEF" }));
  assert.deepEqual(settings, { x: 0, y: 100, zoom: 250, opacity: 0, strength: 0, glass_enabled: true, glass: 50, sharpness: 0, fit: "contain", mask: "#abcdef" });
  const fallback = plain(normalizeAppearance({ zoom: "bad", opacity: "", mask: "url(https://example.invalid)", fit: "invalid" }));
  assert.equal(fallback.zoom, 100);
  assert.equal(fallback.opacity, 85);
  assert.equal(fallback.fit, "cover");
  assert.equal(fallback.mask, "#edf6fc");
  assert.equal(normalizeAppearance({glass_enabled:false,glass:0}).glass_enabled,false);
  assert.equal(normalizeAppearance({glass_enabled:false,glass:0}).glass,0);
  assert.equal(normalizeAppearance({glass:150}).glass,100);
  assert.equal(normalizeAppearance({sharpness:150}).sharpness,100);
  assert.equal(normalizeAppearance({sharpness:-1}).sharpness,0);
});

test("流星参数支持关闭、零亮度、零数量并限制数量和速度范围", () => {
  const {normalizeMeteors} = loadMeteors();
  const zero = normalizeMeteors({meteor_enabled:false,meteor_count:0,meteor_brightness:0,meteor_speed:25});
  assert.equal(zero.meteor_enabled,false);
  assert.equal(zero.meteor_count,0);
  assert.equal(zero.meteor_brightness,0);
  assert.equal(zero.meteor_speed,25);
  const limited = normalizeMeteors({meteor_count:999,meteor_brightness:999,meteor_speed:0});
  assert.equal(limited.meteor_count,256);
  assert.equal(limited.meteor_brightness,100);
  assert.equal(limited.meteor_speed,25);
  assert.equal(normalizeMeteors({meteor_count:12.6}).meteor_count,13);
});

test("流星预览更新数量、亮度、速度和开关，调亮度时保持已有轨迹", () => {
  const style = () => ({setProperty(key,value){this[key]=value;}});
  const layers = Object.fromEntries(["night-meteors","meteor-preview"].map(id => [id,{
    style:style(),children:[],replaceChildren(fragment){this.children=fragment.items;},
  }]));
  const {normalizeMeteors,renderMeteors,meteorLayout} = loadMeteors({document:{
    getElementById:id=>layers[id],
    createDocumentFragment:()=>({items:[],append(item){this.items.push(item);}}),
    createElement:()=>({style:style()}),
  }});
  const settings = normalizeMeteors({meteor_count:256});
  renderMeteors(settings);
  for (const layer of Object.values(layers)) assert.equal(layer.children.length,256);
  const original = layers["night-meteors"].children[0];
  renderMeteors({...settings,meteor_enabled:false,meteor_brightness:0,meteor_speed:200});
  for (const layer of Object.values(layers)) {
    assert.equal(layer.hidden,true);
    assert.equal(layer.style["--meteor-light"],"0");
    assert.equal(layer.style["--meteor-speed"],"2");
  }
  assert.equal(layers["night-meteors"].children[0],original);
  renderMeteors({...settings,meteor_count:0});
  assert.equal(layers["night-meteors"].children.length,0);
  assert.equal(layers["night-meteors"].hidden,false);
  const positions = Array.from({length:256},(_,i)=>meteorLayout(i));
  assert.equal(new Set(positions.map(p=>p.left+","+p.top)).size,256);
  assert.ok(positions.every(p=>parseFloat(p.delay)<0 && parseFloat(p.duration)>0));
});

test("流星保存只调用独立接口，恢复默认不触及插画状态", async () => {
  const calls = [];
  const api = loadMeteors({window:{AstrBotPluginView:{ready:async()=>{},
    apiGet:async path=>{calls.push(path);return {meteor_count:24};},
    apiPost:async (path,body)=>{calls.push(path);assert.deepEqual(Object.keys(body).sort(),["meteor_brightness","meteor_count","meteor_enabled","meteor_speed"]);return {saved:true};},
  }}});
  await api.meteorStorage("readonly");
  await api.meteorStorage("readwrite",api.normalizeMeteors());
  assert.deepEqual(calls,["meteor/state","meteor/save"]);
  const handler = meteorScript.slice(meteorScript.indexOf('meteorEl("meteor-reset").onclick ='),meteorScript.indexOf('meteorEl("meteor-save").onclick ='));
  const button = {};
  const context = {meteorEl:()=>button,meteorBusy:false,draftMeteors:{meteor_count:10},
    draftAppearance:{zoom:170},draftIllustration:"custom-image",fillMeteorControls(){},meteorStatus(){}};
  vm.runInNewContext(meteorHelpers+handler+'; meteorEl("meteor-reset").onclick();',context);
  assert.deepEqual(plain(context.draftMeteors),plain(api.normalizeMeteors()));
  assert.deepEqual(context.draftAppearance,{zoom:170});
  assert.equal(context.draftIllustration,"custom-image");
});

test("锐化在零强度不改变图像，增强边缘时保持平坦区域亮度", () => {
  const {sharpnessKernel} = load();
  assert.deepEqual(sharpnessKernel(0).split(" ").map(Number),[0,0,0,0,1,0,0,0,0]);
  const kernel=sharpnessKernel(100).split(" ").map(Number);
  assert.ok(Math.abs(kernel.reduce((a,b)=>a+b,0)-1)<1e-9);
  const patch=[.4,.4,.4,.4,.5,.4,.4,.4,.4];
  assert.ok(kernel.reduce((value,weight,i)=>value+weight*patch[i],0)>.5);
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
  for (const id of ["btn-run", "btn-login", "tab-main", "tab-rhythm", "tab-upload", "tab-delivery", "tab-comments", "tab-reply", "f-speaking_style_bind_persona", "appearance-dialog", "art-file", "art-save", "art-cancel", "art-meteor_enabled", "art-meteor_count", "art-meteor_brightness", "art-meteor_speed"]) assert.ok(ids.includes(id), id);
  assert.match(html, /<dialog[^>]*aria-labelledby="appearance-title"/);
  assert.match(html, /prefers-reduced-motion/);
  assert.equal((html.match(/<\/body>/g) || []).length, 1);
});

test("恢复默认仅重置参数，保留当前图片或无插画状态，忙碌时不重置", () => {
  const handler = script.slice(script.indexOf('artEl("art-reset").onclick ='), script.indexOf('artEl("art-save").onclick ='));
  for (const image of ["data:image/png;base64,current-custom-image", null]) {
    const button = {};
    const context = { artEl: () => button, artBusy: false, draftAppearance: {zoom:180,glass:0}, draftIllustration:image, fillAppearanceControls(){}, artStatus(){} };
    vm.runInNewContext(helpers + handler + '; artEl("art-reset").onclick();', context);
    assert.equal(context.draftIllustration,image);
    assert.deepEqual(plain(context.draftAppearance),plain(load().normalizeAppearance()));
    context.artBusy=true;
    context.draftAppearance={zoom:180};
    button.onclick();
    assert.deepEqual(plain(context.draftAppearance),{zoom:180});
  }
});
