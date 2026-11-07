import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../pages/panel/index.html", import.meta.url), "utf8");
const sync = html.slice(html.indexOf("function syncAutoPublishActions()"), html.indexOf("function renderAutoPublish("));
const modelOptions = html.slice(html.indexOf("function setAutoPublishModelOptions("), html.indexOf("function selectAutoPublishProfile()"));

function form() {
  const nodes = {};
  for (const name of ["provider", "region", "model", "key", "time", "limit", "direction", "tid", "workspace", "duration", "enabled", "category", "draft-hint", "tid-label", "tid-tip", "key-state", "clear", "start"]) nodes["u-auto-" + name] = {};
  const settings = {provider:"minimax",region:"cn",enabled:true,auto_category:false,tid:21,direction:"鱼吃饭",time:"20:00",limit:1};
  for (const key of ["provider", "region", "tid", "time", "direction"]) nodes["u-auto-" + key].value = String(settings[key]);
  for (const key of ["key", "model", "workspace"]) nodes["u-auto-" + key].value = "";
  nodes["u-auto-enabled"].checked = true;
  nodes["u-auto-category"].checked = false;
  nodes["u-auto-limit"].valueAsNumber = 1;
  nodes["u-auto-duration"].value = "6";
  const context = {$:id=>nodes[id],autoPublishState:{settings,jobs:[],logged_in:true,profiles:{"minimax:cn":{key_configured:true,model:"",duration:6}},providers:{minimax:{duration:6}}},autoPublishActive:["queued","requesting","generating","ready","uploading"]};
  vm.runInNewContext(sync, context);
  return {nodes,context};
}

test("自主投稿的全部可编辑设置有改动时，禁止立即生成并提示先保存", () => {
  for (const [name, property, value] of [
    ["direction","value","新选题"],["limit","valueAsNumber",2],["time","value","21:00"],
    ["workspace","value","workspace-1"],["key","value","new-key"],["model","value","T2V-01"],
    ["category","checked",true],["tid","value","17"],["enabled","checked",false],
    ["duration","value","10"],
  ]) {
    const {nodes,context} = form();
    context.syncAutoPublishActions();
    assert.equal(nodes["u-auto-start"].disabled,false);
    const previous = nodes["u-auto-" + name][property];
    nodes["u-auto-" + name][property] = value;
    context.syncAutoPublishActions();
    assert.equal(nodes["u-auto-start"].disabled,true,name);
    assert.match(nodes["u-auto-draft-hint"].textContent,/先保存/,name);
    nodes["u-auto-" + name][property] = previous;
    context.syncAutoPublishActions();
    assert.equal(nodes["u-auto-start"].disabled,false,name);
  }
});

test("接口没有返回视频模型时，保留已适配选项并明确权限未验证", () => {
  let actual = [];
  const nodes={"u-auto-provider":{value:"minimax"},"u-auto-model":{replaceChildren:(...items)=>actual=items},"u-auto-duration":{value:"6",replaceChildren(){}},"u-auto-duration-tip":{}};
  const context = {$:id=>nodes[id],autoPublishState:{providers:{minimax:{duration:6,model:"MiniMax-Hailuo-2.3",models:["MiniMax-Hailuo-2.3","T2V-01"]}}},Option:function(text,value){this.text=text;this.value=value;}};
  vm.runInNewContext(modelOptions,context);
  context.setAutoPublishModelOptions("",[]);
  assert.ok(actual.some(option=>option.value==="T2V-01" && option.text.includes("权限未验证")));
  context.setAutoPublishModelOptions("T2V-01",[{id:"T2V-01",name:"test"}]);
  assert.equal(actual.filter(option=>option.value==="T2V-01").length,1);
  assert.equal(nodes["u-auto-model"].value,"T2V-01");
});

test("切换到短时长模型时收回不支持的选项，不把较长时长带入新模型", () => {
  let actual=[];
  const nodes={"u-auto-provider":{value:"minimax"},"u-auto-model":{value:"MiniMax-Hailuo-2.3"},"u-auto-duration":{value:"10",replaceChildren:(...items)=>actual=items},"u-auto-duration-tip":{}};
  const context={$:id=>nodes[id],autoPublishState:{providers:{minimax:{duration:6,model:"MiniMax-Hailuo-2.3",duration_rules:[{prefix:"MiniMax-Hailuo-",options:[6,10]},{prefix:"T2V-",options:[6]}]}}},Option:function(text,value){this.text=text;this.value=value;}};
  vm.runInNewContext(modelOptions,context);
  context.syncAutoPublishDuration();
  assert.equal(nodes["u-auto-duration"].value,"10");
  nodes["u-auto-model"].value="T2V-01";
  context.syncAutoPublishDuration();
  assert.deepEqual(actual.map(option=>option.value),["6"]);
  assert.equal(nodes["u-auto-duration"].value,"6");
  assert.match(nodes["u-auto-duration-tip"].textContent,/仅支持 6 秒/);
});
