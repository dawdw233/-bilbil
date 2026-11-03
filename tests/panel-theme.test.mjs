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
