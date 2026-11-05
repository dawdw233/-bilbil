import test from "node:test";
import assert from "node:assert/strict";
import {readFileSync} from "node:fs";
import vm from "node:vm";

const html=readFileSync(new URL("../pages/panel/index.html",import.meta.url),"utf8");
const script=html.match(/<script type="module" id="theme-script">([\s\S]*?)<\/script>/)[1];
const context={};
vm.runInNewContext(script.slice(0,script.indexOf('const themeModeControl'))+'; globalThis.resolve=studyThemeFor;',context);

test("随时间模式按北京时间在 19:00 和次日 07:00 切换",()=>{
  for (const [date,expected] of [
    ['2026-11-03T10:59:59Z','light'], ['2026-11-03T11:00:00Z','night'],
    ['2026-11-03T15:59:59Z','night'], ['2026-11-03T16:00:00Z','night'],
    ['2026-11-03T22:59:59Z','night'], ['2026-11-03T23:00:00Z','light'],
    ['2026-11-04T04:00:00Z','light']]) assert.equal(context.resolve('auto',new Date(date)),expected,date);
});

test("手动选择浅色或夜晚不受时间影响",()=>{
  for (const date of ['2026-11-03T04:00:00Z','2026-11-03T16:00:00Z']) {
    assert.equal(context.resolve('light',new Date(date)),'light');
    assert.equal(context.resolve('night',new Date(date)),'night');
  }
});

function themePage(apiGet) {
  const root={dataset:{},loading:true,removeAttribute(){this.loading=false;}};
  const control={disabled:true};
  const focus={};
  const elements={"theme-mode":control,"meteor-dialog":{open:false},"theme-feedback":{classList:{add(){},remove(){}}}};
  const timers=new Map();
  let timerId=0;
  vm.runInNewContext(script,{
    Date:class extends Date { constructor(){super("2026-11-05T04:00:00Z");} },
    document:{documentElement:root,getElementById:id=>elements[id],addEventListener(){}},
    window:{AstrBotPluginView:{ready:async()=>{},apiGet},addEventListener:(event,listener)=>{focus[event]=listener;}},
    setTimeout:callback=>{timers.set(++timerId,callback);return timerId;},
    clearTimeout:id=>timers.delete(id),
    setInterval(){},
  });
  return {root,control,focus:()=>focus.focus(),expireRead:()=>timers.get(1)()};
}

test("白天打开保存了夜晚模式的面板，等待读取期间不显示浅色控件",async()=>{
  let resolveRead;
  const page=themePage(()=>new Promise(resolve=>{resolveRead=resolve;}));
  await new Promise(setImmediate);
  page.focus();
  assert.equal(page.root.loading,true);
  assert.equal(page.root.dataset.studyTheme,undefined);
  resolveRead({mode:"night"});
  await new Promise(setImmediate);
  assert.equal(page.root.dataset.studyTheme,"night");
  assert.equal(page.root.loading,false);
  assert.equal(page.control.disabled,false);
});

test("主题读取超时后面板可用，重新聚焦时恢复已保存的夜晚模式",async()=>{
  let reads=0;
  let resolveSlowRead;
  const page=themePage(()=>++reads===1
    ? new Promise(resolve=>{resolveSlowRead=resolve;})
    : Promise.resolve({mode:"night"}));
  await new Promise(setImmediate);
  page.expireRead();
  await new Promise(setImmediate);
  assert.equal(page.root.loading,false);
  assert.equal(page.control.disabled,false);
  page.focus();
  await new Promise(setImmediate);
  resolveSlowRead({mode:"light"});
  await new Promise(setImmediate);
  assert.equal(page.root.dataset.studyTheme,"night");
  assert.equal(reads,2);
});

test("切回浅色时收起流星面板，夜晚时保留面板并显示入口",()=>{
  const dialog={open:true,closes:0,close(){this.open=false;this.closes++;}};
  const sandbox={currentThemeMode:"night",studyThemeFor:mode=>mode,themeModeControl:{},
    document:{documentElement:{dataset:{}},getElementById:id=>{assert.equal(id,"meteor-dialog");return dialog;}}};
  const apply=script.slice(script.indexOf("function applyStudyTheme()"),script.indexOf("function themeFeedback("));
  vm.runInNewContext(apply+";globalThis.apply=applyStudyTheme;",sandbox);
  sandbox.apply();
  assert.equal(sandbox.document.documentElement.dataset.studyTheme,"night");
  assert.equal(dialog.open,true);
  sandbox.currentThemeMode="light";
  sandbox.apply();
  assert.equal(dialog.open,false);
  assert.equal(dialog.closes,1);
  sandbox.apply();
  assert.equal(dialog.closes,1);
  assert.match(html,/:root:not\(\[data-study-theme="night"\]\) #meteor-open \{ display: none; \}/);
});

test("丁达尔光束只在浅色模式显示，不挡点击，减少动态效果时静止",()=>{
  assert.match(html,/<div class="light-tyndall" aria-hidden="true"><\/div>\n<div class="light-tyndall-rays" aria-hidden="true"><i class="tyndall-dust"><\/i><\/div>/);
  assert.match(html,/\.light-tyndall, \.light-tyndall-rays \{ display: none; position: fixed; inset: 0; overflow: hidden; pointer-events: none; \}/);
  assert.match(html,/:root:not\(\[data-study-theme="night"\]\) :is\(\.light-tyndall, \.light-tyndall-rays\) \{ display: block; \}/);
  assert.match(html,/@media \(prefers-reduced-motion: reduce\) \{ \.light-tyndall-rays::before, \.light-tyndall-rays::after, \.tyndall-dust::before, \.tyndall-dust::after \{ animation: none; \} \}/);
});
