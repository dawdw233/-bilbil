import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const html = readFileSync(new URL("../pages/panel/index.html", import.meta.url), "utf8");
const sync = html.slice(html.indexOf("function syncAutoPublishActions()"), html.indexOf("function renderAutoPublish("));
const modelOptions = html.slice(html.indexOf("function setAutoPublishModelOptions("), html.indexOf("function selectAutoPublishProfile()"));

function form() {
  const nodes = {};
  for (const name of ["provider", "region", "model", "key", "time", "limit", "direction", "tid", "workspace", "duration", "mode", "total", "link", "link-field", "link-tip", "duration-field", "total-field", "storyboard-tip", "enabled", "category", "reference", "reference-dir", "reference-status", "reference-model", "reference-upload", "reference-refresh", "reference-upload-status", "reference-file", "fields", "draft-hint", "tid-label", "tid-tip", "key-state", "clear", "start"]) nodes["u-auto-" + name] = {};
  const settings = {provider:"minimax",region:"cn",enabled:true,auto_category:false,tid:21,direction:"鱼吃饭",time:"20:00",limit:1};
  for (const key of ["provider", "region", "tid", "time", "direction"]) nodes["u-auto-" + key].value = String(settings[key]);
  for (const key of ["key", "model", "workspace"]) nodes["u-auto-" + key].value = "";
  nodes["u-auto-enabled"].checked = true;
  nodes["u-auto-category"].checked = false;
  nodes["u-auto-reference"].checked = false;
  nodes["u-auto-limit"].valueAsNumber = 1;
  nodes["u-auto-duration"].value = "6";
  nodes["u-auto-mode"].value = "single";
  nodes["u-auto-total"].value = "30";
  nodes["u-auto-link"].value = "independent";
  for(const name of ["base-url","transport","native-audio"])nodes["u-auto-"+name]={value:name==="transport"?"multipart":"",checked:false};
  for (const key of ["duration-field","total-field","storyboard-tip","link-field"]) nodes["u-auto-"+key].classList={toggle(){}};
  const context = {$:id=>nodes[id],voiceSaving:false,voiceDraftDirty:false,autoReferenceUploading:false,autoPublishSaving:false,autoPublishRequest:0,autoPublishState:{settings,jobs:[],logged_in:true,profiles:{"minimax:cn":{key_configured:true,model:"",duration:6}},providers:{minimax:{duration:6}}},autoPublishActive:["queued","requesting","generating","voiceover","ready","uploading"]};
  context.setHint=(id,message)=>nodes[id].textContent=message;
  context.syncCharacterAudioHint=()=>{};
  vm.runInNewContext(sync, context);
  return {nodes,context};
}

test("自主投稿的全部可编辑设置有改动时，禁止立即生成并提示先保存", () => {
  for (const [name, property, value] of [
    ["direction","value","新选题"],["limit","valueAsNumber",2],["time","value","21:00"],
    ["workspace","value","workspace-1"],["key","value","new-key"],["model","value","T2V-01"],
    ["category","checked",true],["tid","value","17"],["enabled","checked",false],
    ["duration","value","10"],
    ["mode","value","storyboard"],["total","value","60"],["reference","checked",true],
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

test("分镜估算按模型合法时长组合 30/60 秒，并显示分别生成和计费", () => {
  const {nodes,context}=form();
  context.autoPublishState.providers.minimax.duration_rules=[{prefix:"MiniMax-Hailuo-",options:[6,10]}];
  context.autoPublishState.providers.minimax.model="MiniMax-Hailuo-2.3";
  nodes["u-auto-mode"].value="storyboard";
  context.syncAutoPublishActions();
  assert.match(nodes["u-auto-storyboard-tip"].textContent,/3 个分镜/);
  assert.match(nodes["u-auto-storyboard-tip"].textContent,/分别生成和计费/);
  assert.deepEqual(Array.from(context.storyboardLengths([6,10],60)),[10,10,10,10,10,10]);
  assert.deepEqual(Array.from(context.storyboardLengths([5,10,15],30)),[15,15]);
});

test("尾帧接续独立保存，未保存及不支持图生的模型阻止立即运行", () => {
  const {nodes,context}=form();
  const state=context.autoPublishState;
  state.settings.mode="storyboard";
  nodes["u-auto-mode"].value="storyboard";
  state.providers.minimax.model="MiniMax-Hailuo-2.3";
  state.providers.minimax.duration_rules=[{prefix:"MiniMax-Hailuo-",options:[6,10]}];
  state.providers.minimax.reference_models={"MiniMax-Hailuo-2.3":"MiniMax-Hailuo-2.3"};
  nodes["u-auto-link"].value="continue";
  context.syncAutoPublishActions();
  assert.equal(nodes["u-auto-start"].disabled,true);
  assert.match(nodes["u-auto-draft-hint"].textContent,/先保存/);
  state.settings.shot_link="continue";
  context.syncAutoPublishActions();
  assert.equal(nodes["u-auto-start"].disabled,false);
  nodes["u-auto-model"].value="T2V-01";
  state.profiles["minimax:cn"].model="T2V-01";
  context.syncAutoPublishActions();
  assert.equal(nodes["u-auto-start"].disabled,true);
  assert.match(nodes["u-auto-link-tip"].textContent,/不支持尾帧接续/);
});

test("视频地址、请求格式及原生声音能力有修改时，先保存再运行",()=>{
  for(const [name,property,value] of [["base-url","value","https://gateway.test/v1"],["transport","value","json"],["native-audio","checked",true]]){
    const {nodes,context}=form();context.syncAutoPublishActions();assert.equal(nodes['u-auto-start'].disabled,false);
    nodes['u-auto-'+name][property]=value;context.syncAutoPublishActions();
    assert.equal(nodes['u-auto-start'].disabled,true);assert.match(nodes['u-auto-draft-hint'].textContent,/先保存/);
  }
});

test("分镜方案在表格外展示、轮询保留展开状态，并转义模型文案", () => {
  const source=html.slice(html.indexOf("function renderStoryPlans("),html.indexOf("function renderAutoPublish("));
  const container={querySelectorAll:()=>[{dataset:{storyJob:"a".repeat(32)}}],innerHTML:""};
  const context={$:()=>container,esc:value=>String(value).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll('"',"&quot;")};
  vm.runInNewContext(source,context);
  context.renderStoryPlans([{id:"a".repeat(32),title:"<script>bad</script>",shots:[{number:2,duration:10,phase:"done",linked:true,start_state:"已走到窗前",narration:"",prompt:'<img src="x">'}]}]);
  assert.match(container.innerHTML,/ open>/);
  assert.match(container.innerHTML,/已接续上一镜尾帧/);
  assert.match(container.innerHTML,/旁白：留白/);
  assert.doesNotMatch(container.innerHTML,/<script>|<img/);
  assert.match(html,/<\/table>\s*<\/div>\s*<div id="u-auto-plans">/);
});

test("参考图显示真实目录与图集状态，空图集及不支持的模型阻止生成，关闭后恢复文字模式", () => {
  const {nodes,context}=form();
  const state=context.autoPublishState;
  state.settings.reference_enabled=true;
  nodes["u-auto-reference"].checked=true;
  state.providers.minimax.model="MiniMax-Hailuo-2.3";
  state.providers.minimax.reference_models={"MiniMax-Hailuo-2.3":"MiniMax-Hailuo-2.3"};
  state.references={directory:"/data/reference_images",count:0,files:[],invalid_count:0,error:""};
  context.syncAutoPublishActions();
  assert.equal(nodes["u-auto-start"].disabled,true);
  assert.match(nodes["u-auto-reference-status"].textContent,/空图集不会生成/);
  state.references.count=1;
  state.references.files=[{name:"<unsafe>.png"}];
  context.syncAutoPublishActions();
  assert.equal(nodes["u-auto-start"].disabled,false);
  assert.equal(nodes["u-auto-reference-dir"].textContent,"/data/reference_images");
  assert.match(nodes["u-auto-reference-model"].textContent,/识图策划模型/);
  state.references.error="无法读取参考图文件夹";
  context.syncAutoPublishActions();
  assert.equal(nodes["u-auto-start"].disabled,true);
  state.references.error="";
  nodes["u-auto-model"].value="T2V-01";
  state.profiles["minimax:cn"].model="T2V-01";
  context.syncAutoPublishActions();
  assert.equal(nodes["u-auto-start"].disabled,true);
  assert.match(nodes["u-auto-reference-model"].textContent,/未适配/);
  state.settings.reference_enabled=false;
  nodes["u-auto-reference"].checked=false;
  context.syncAutoPublishActions();
  assert.equal(nodes["u-auto-start"].disabled,false);
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

test("本机图集上传逐张验证、确认保存、复用重复图，部分失败可见且不会自动开启参考图", async () => {
  const {nodes,context}=form();
  const calls=[];
  context.FileReader=function(){this.readAsDataURL=file=>{this.result="data:"+file.type+";base64,fixture";this.onload();};};
  context.api=async(path,body)=>{
    calls.push([path,body.name]);
    return {uploaded:true,created:body.name!=="existing.jpg",references:{count:2,files:[{name:"new.png"},{name:"existing.jpg"}]}};
  };
  await context.uploadAutoPublishReferences([{name:"bad.bmp",type:"image/bmp",size:10},{name:"new.png",type:"image/png",size:30},{name:"existing.jpg",type:"image/jpeg",size:30}]);
  assert.deepEqual(calls.map(item=>item[1]),["new.png","existing.jpg"]);
  assert.ok(calls.every(item=>item[0]==="upload/auto/reference/upload"));
  assert.match(nodes["u-auto-reference-upload-status"].textContent,/已上传 1 张，图集已有 1 张，1 张未成功/);
  assert.equal(nodes["u-auto-reference"].checked,false);
  assert.equal(nodes["u-auto-fields"].disabled,false);
  assert.equal(nodes["u-auto-reference-upload"].disabled,false);
  assert.equal(nodes["u-auto-reference-file"].value,"");
  context.api=async()=>({uploaded:false});
  await context.uploadAutoPublishReferences([{name:"new.png",type:"image/png",size:30}]);
  assert.match(nodes["u-auto-reference-upload-status"].textContent,/服务器未确认保存/);
  context.FileReader=function(){throw new Error("Should not read an oversized file");};
  await context.uploadAutoPublishReferences([{name:"big.png",type:"image/png",size:11*1024*1024}]);
  assert.match(nodes["u-auto-reference-upload-status"].textContent,/10 MB/);
});

test("上传中禁止重复提交与立即生成，取消选择不发请求，结束后恢复操作", async () => {
  const {nodes,context}=form();
  let calls=0,finish;
  context.FileReader=function(){this.readAsDataURL=()=>{this.result="data:image/png;base64,fixture";this.onload();};};
  context.api=()=>{calls++;return new Promise(resolve=>finish=resolve);};
  await context.uploadAutoPublishReferences([]);
  assert.equal(calls,0);
  const pending=context.uploadAutoPublishReferences([{name:"fish.png",type:"image/png",size:20}]);
  await Promise.resolve();await Promise.resolve();
  assert.equal(nodes["u-auto-start"].disabled,true);
  await context.uploadAutoPublishReferences([{name:"fish.png",type:"image/png",size:20}]);
  assert.equal(calls,1);
  finish({uploaded:true,created:true,references:{count:1,files:[{name:"fish.png"}]}});
  await pending;
  assert.equal(nodes["u-auto-start"].disabled,false);
});

test("MiniMax 连接检查区分未列出视频模型和接口拒绝，不把其他服务改为连接检查", async () => {
  const nodes = Object.fromEntries(["provider", "region", "model", "model-tip", "model-read", "key", "workspace", "base-url"].map(name => ["u-auto-" + name, {value:""}]));
  nodes["u-auto-provider"].value = "minimax";
  nodes["u-auto-region"].value = "cn";
  let calls = 0;
  const message = "MiniMax 列表接口已响应，但未列出海螺视频模型。视频生成权限尚未验证。";
  const context = {$:id=>nodes[id], autoModelRequest:0,
    autoPublishState:{profiles:{}}, setAutoPublishModelOptions(){}, syncAutoPublishActions(){},
    api:async path=>{assert.equal(path,"upload/auto/models");calls++;return {models:[],message};}};
  vm.runInNewContext(html.slice(html.indexOf("function resetAutoPublishModels()"), html.indexOf("function setAutoPublishModelOptions("))
    + html.slice(html.indexOf("async function readAutoPublishModels()"), html.indexOf("function syncAutoPublishActions()")), context);
  context.resetAutoPublishModels();
  assert.equal(nodes["u-auto-model-read"].textContent,"检查连接");
  await context.readAutoPublishModels();
  assert.equal(calls,1);
  assert.equal(nodes["u-auto-model-tip"].textContent,message);
  assert.equal(nodes["u-auto-model-read"].disabled,false);
  context.api = async()=>{throw new Error("请检查 Key 与地区");};
  await context.readAutoPublishModels();
  assert.match(nodes["u-auto-model-tip"].textContent,/连接检查失败：请检查 Key 与地区/);
  nodes["u-auto-provider"].value="wan";
  context.resetAutoPublishModels();
  assert.equal(nodes["u-auto-model-read"].textContent,"读取模型");
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

test("投稿成功后同时展示 BV 链接和本地清理状态，并转义错误内容", () => {
  const source=html.slice(html.indexOf("function autoPublishResultMarkup("),html.indexOf("function renderAutoPublish("));
  const context={esc:value=>String(value).replaceAll("&","&amp;").replaceAll("<","&lt;").replaceAll(">","&gt;")};
  vm.runInNewContext(source,context);
  const result=context.autoPublishResultMarkup({bvid:"BV1234567890",file:"ai-example.mp4",message:"本地成片已清理。<script>"});
  assert.match(result,/href="https:\/\/www.bilibili.com\/video\/BV1234567890"/);
  assert.match(result,/本地成片已清理/);
  assert.ok(!result.includes("<script>"));
  assert.equal(context.autoPublishResultMarkup({}),"—");
});
