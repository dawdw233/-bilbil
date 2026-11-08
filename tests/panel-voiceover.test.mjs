import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
const html=readFileSync(new URL('../pages/panel/index.html',import.meta.url),'utf8');
const source=html.slice(html.indexOf('function voicePayload()'),html.indexOf('async function loadAutoPublish('));
function setup() {
  const nodes={};
  for(const name of ['enabled','provider','region','key','model','voice','speed','mode','original_volume','voice_volume','subtitles','font_size','key-tip','clear','draft','fields','feedback','form','kind','style','style-field','capability','asr-key','asr-tip','asr-key-field','asr-workspace','asr-workspace-field','legacy'])nodes['u-voice-'+name]={value:'',checked:false,addEventListener(){},replaceChildren(){},classList:{toggle(){}}};
  nodes['u-auto-provider']={value:'minimax'};
  for(const name of ['asr-service','asr-model','asr-base','asr-service-field','asr-model-field','asr-base-field','asr-protocol-tip'])nodes['u-voice-'+name]={value:'',replaceChildren(){},classList:{toggle(){}}};
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

test('角色声音模式不使用旧TTS表单，提示真实视频能力并在关闭字幕时停用识别输入',()=>{
  const {nodes,c,state}=setup();
  nodes['u-auto-model']={value:''};
  c.autoPublishState={providers:{minimax:{model:'MiniMax-Hailuo-2.3',native_audio_prefixes:[]},wan:{model:'wan2.6-t2v',native_audio_prefixes:['wan2.5-','wan2.6-']}}};
  c.syncCharacterAudioHint();
  assert.match(nodes['u-voice-capability'].textContent,/不会用旁白代替/);
  nodes['u-auto-provider'].value='wan';c.syncCharacterAudioHint();
  assert.match(nodes['u-voice-capability'].textContent,/支持原生有声/);
  nodes['u-voice-kind'].value='original';c.syncCharacterAudioHint();
  assert.match(nodes['u-voice-capability'].textContent,/不会添加角色声音、改口型/);
  nodes['u-voice-subtitles'].checked=false;c.syncVoiceoverForm();
  assert.equal(nodes['u-voice-asr-key'].disabled,true);
  assert.equal(nodes['u-voice-asr-workspace'].disabled,true);
  assert.equal(c.voicePayload().mode,'original');
});

test('识别Key与旧配音Key独立，清除只针对当前模式，保存失败保留识别草稿',async()=>{
  const {nodes,c,state}=setup();
  nodes['u-voice-asr-key'].value='draft-asr';c.syncVoiceoverForm();
  assert.equal(c.voiceDraftDirty,true);
  c.api=async()=>{throw new Error('保存失败');};
  await c.saveVoiceover(c.voicePayload());
  assert.equal(nodes['u-voice-asr-key'].value,'draft-asr');
  let captured;
  c.api=async(_path,body)=>{captured=body;return {voiceover:state};};
  await nodes['u-voice-clear'].onclick();
  // The click handler intentionally launches saveVoiceover asynchronously.
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(captured.remove_asr_key,true);
  assert.equal(captured.remove_key,undefined);
  assert.equal(captured.enabled,false);
});

test('切换识别协议读取各自地址和模型，清空Key草稿并保留其他服务配置',()=>{
  const {nodes,c,state}=setup();
  state.asr_profiles={paraformer:{key_configured:true,base_url:'wss://para.test/api-ws/v1/inference',model:'para-model',workspace:'space'},openai:{key_configured:true,base_url:'https://whisper.test/v1',model:'whisper-custom',workspace:''}};
  nodes['u-voice-asr-key'].value='unsaved-key';
  nodes['u-voice-asr-service'].value='openai';c.selectRecognitionService();
  assert.equal(nodes['u-voice-asr-key'].value,'');
  assert.equal(nodes['u-voice-asr-base'].value,'https://whisper.test/v1');
  assert.equal(nodes['u-voice-asr-model'].value,'whisper-custom');
  assert.equal(c.voicePayload().asr_service,'openai');
  nodes['u-voice-asr-service'].value='paraformer';c.selectRecognitionService();
  assert.equal(nodes['u-voice-asr-model'].value,'para-model');assert.equal(nodes['u-voice-asr-workspace'].value,'space');
  assert.equal(state.asr_profiles.openai.base_url,'https://whisper.test/v1');
});
