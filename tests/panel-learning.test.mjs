import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html=readFileSync(new URL('../pages/panel/index.html',import.meta.url),'utf8');
const source=html.slice(html.indexOf('let learningState='),html.indexOf('async function loadUpload()'));
const state=()=>({settings:{enabled:false,daily_limit:2,frame_count:4},records:[],today_attempts:0,lesson_count:0,busy:false});
function setup(){
 const nodes={};
 for(const id of ['form','enabled','limit','frames','fields','draft','feedback','summary','records','more','refresh'])nodes['learn-'+id]={addEventListener(){},querySelectorAll(){return []}};
 const context={$:id=>nodes[id],api:async()=>state(),confirm:()=>false,esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;')};
 context.setHint=(id,text)=>{nodes[id].textContent=text};
 vm.createContext(context);vm.runInContext(source,context);return {nodes,context};
}
test('读取失败后可刷新，首次读取不开启学习，刷新不覆盖未保存设置',async()=>{
 const {nodes,context}=setup();context.api=async()=>{throw new Error('offline')};
 await context.loadLearning();assert.match(nodes['learn-feedback'].textContent,/读取学习状态失败/);
 assert.equal(nodes['learn-refresh'].disabled,false);
 context.api=async()=>state();await context.loadLearning();
 assert.equal(nodes['learn-enabled'].checked,false);
 nodes['learn-enabled'].checked=true;context.syncLearningDraft();
 await context.loadLearning();assert.equal(nodes['learn-enabled'].checked,true);
 assert.match(nodes['learn-draft'].textContent,/尚未保存/);
});
test('保存失败保留草稿，重试成功才更新已保存设置',async()=>{
 const {nodes,context}=setup();await context.loadLearning();
 nodes['learn-enabled'].checked=true;
 context.api=async()=>{throw new Error('save failed')};
 await nodes['learn-form'].onsubmit({preventDefault(){}});
 assert.equal(nodes['learn-enabled'].checked,true);assert.equal(nodes['learn-fields'].disabled,false);
 assert.match(nodes['learn-feedback'].textContent,/保存失败/);
 let payload;context.api=async(path,body)=>{assert.equal(path,'learning/save');payload=body;return {...state(),settings:body}};
 await nodes['learn-form'].onsubmit({preventDefault(){}});
 assert.equal(payload.enabled,true);assert.equal(nodes['learn-draft'].textContent,'');
});
test('经验内容转义，未验证状态和不适用反馈可见，删除需指明后果',async()=>{
 const {nodes,context}=setup();let data=state();
 data.records=[{id:'a'.repeat(32),bvid:'BV1234567890',phase:'done',title:'<img onerror=bad>',created_at:'2026-11-08T19:00',declaration:{source:'平台声明',text:'视频由AI生成'},lessons:[{id:'b'.repeat(32),status:'pending',topic:'吃饭',rule:'<script>bad</script>',evidence:'两帧显示勺子移动',limitation:'不能确认完整动作',tags:['吃饭'],frames:[1,2]}]}];
 context.api=async()=>data;await context.loadLearning();
 const markup=nodes['learn-records'].innerHTML;
 assert.ok(!markup.includes('<script>'));assert.ok(!markup.includes('<img onerror'));
 assert.match(markup,/待验证/);assert.match(markup,/不适用/);
 let calls=0;context.api=async()=>{calls++;return state()};
 const button={dataset:{learnAction:'delete',learnRecord:'a'.repeat(32)}};
 await nodes['learn-records'].onclick({target:{closest:()=>button}});assert.equal(calls,0);
 context.confirm=message=>{assert.match(message,/不会自动重新学习/);return true};
 await nodes['learn-records'].onclick({target:{closest:()=>button}});assert.equal(calls,1);
 assert.match(nodes['learn-feedback'].textContent,/判重和今日用量保留/);
});
