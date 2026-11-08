import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html=readFileSync(new URL('../pages/panel/index.html',import.meta.url),'utf8');
const source=html.slice(html.indexOf('function voicePayload()'),html.indexOf('async function loadAutoPublish('));
function setup() {
  const nodes={};
  for(const name of ['enabled','provider','region','key','model','voice','speed','mode','original_volume','voice_volume','subtitles','font_size','key-tip','clear','draft','fields','feedback','form'])nodes['u-voice-'+name]={value:'',checked:false,addEventListener(){},replaceChildren(){}};
  nodes['u-auto-provider']={value:'minimax'};
  const state={settings:{enabled:false,provider:'minimax',region:'cn',mix:false,original_volume:15,voice_volume:100,subtitles:true,font_size:28},
    providers:{minimax:{name:'MiniMax',models:['speech-2.6-turbo'],voices:{warm:'温柔女声',youth:'青年'},regions:{cn:'中国站',global:'国际站'}},qwen:{name:'千问',models:['qwen3-tts-flash'],voices:{Cherry:'芊悦'},regions:{cn:'中国内地',global:'新加坡'}}},
    profiles:{'minimax:cn':{key_configured:true,model:'speech-2.6-turbo',voice:'warm',speed:1}}};
  const c={$:id=>nodes[id],voiceState:null,voiceLoaded:false,voiceSaving:false,voiceDraftDirty:false,Option:function(text,value){this.text=text;this.value=value;},syncAutoPublishActions(){},renderAutoPublish(){},setHint:(id,text)=>nodes[id].textContent=text};
  vm.runInNewContext(source,c);
  c.renderVoiceover(state,true);
  return {nodes,c,state};
}
test('配音独立设置默认关闭，未保存修改、空Key沿用和服务地区切换清除输入',()=>{
  const {nodes,c}=setup();
  assert.equal(c.voiceDraftDirty,false);
  nodes['u-voice-enabled'].checked=true;c.syncVoiceoverForm();
  assert.equal(c.voiceDraftDirty,true);
  assert.match(nodes['u-voice-draft'].textContent,/尚未保存/);
  nodes['u-voice-key'].value='unsaved-secret';
  nodes['u-voice-provider'].value='qwen';c.selectVoiceRegions('global');
  assert.equal(nodes['u-voice-key'].value,'');
  assert.equal(nodes['u-voice-speed'].disabled,true);
  assert.equal(nodes['u-voice-region'].value,'global');
  assert.equal(nodes['u-voice-model'].value,'qwen3-tts-flash');
});
test('混音与字幕参数独立启停，payload只写配音接口，不改变视频选项',()=>{
  const {nodes,c}=setup();
  assert.equal(nodes['u-voice-original_volume'].disabled,true);
  nodes['u-voice-mode'].value='mix';nodes['u-voice-subtitles'].checked=false;c.syncVoiceoverForm();
  assert.equal(nodes['u-voice-original_volume'].disabled,false);
  assert.equal(nodes['u-voice-font_size'].disabled,true);
  const body=c.voicePayload();assert.equal(body.mix,true);assert.equal(body.subtitles,false);
  assert.equal(body.provider,'minimax');assert.equal(nodes['u-auto-provider'].value,'minimax');
});
test('配音保存失败保留表单草稿并恢复按钮，不误报成功',async()=>{
  const {nodes,c}=setup();
  nodes['u-voice-key'].value='draft';
  c.api=async(path)=>{assert.equal(path,'upload/auto/voice/save');throw new Error('network failed');};
  await c.saveVoiceover(c.voicePayload());
  assert.match(nodes['u-voice-feedback'].textContent,/保存失败/);
  assert.equal(nodes['u-voice-key'].value,'draft');
  assert.equal(nodes['u-voice-fields'].disabled,false);
  assert.equal(c.voiceDraftDirty,true);
});
