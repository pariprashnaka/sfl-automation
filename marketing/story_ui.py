"""HTML/JS for the Cartoon story panel on the Create page, plus the lightbox used by every preview."""
import json
from . import cartoon, shotslides, shots

EXAMPLE = "do dukandaar baat kar rahe hain, ek ka maal expire hota hai, dusra solution batata hai"

STYLE = """<style>
.mk .lb{position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:200;display:flex;align-items:center;justify-content:center;gap:12px}
.mk .lb[hidden]{display:none!important}
.mk .lb img{max-height:92vh;max-width:min(92vw,900px);border-radius:12px;box-shadow:0 8px 40px rgba(0,0,0,.6)}
.mk .lb .x{position:absolute;top:16px;right:20px}.mk .lb .nav{background:var(--surface2);color:var(--text);border:1px solid var(--border)}
.mk #imgs img{cursor:zoom-in}
.mk .sl{border:1px solid var(--border);border-radius:14px;padding:14px;margin:12px 0;background:var(--surface2)}
.mk .sl h4{margin:0 0 8px;display:flex;justify-content:space-between;align-items:center}
.mk .sl select,.mk .sl input,.mk .sl textarea{margin:4px 0}.mk .sl .inl{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.mk .sl .inl>*{flex:1;min-width:110px}.mk .sl .inl>button{flex:none;min-width:0}
.mk .warn{background:rgba(245,165,36,.16);border:1px solid #f5c542;color:#ffd666;border-radius:10px;padding:8px 12px;margin:8px 0;font-size:13px}
.mk .props label{display:inline-flex;align-items:center;margin:2px 10px 2px 0;color:var(--text)}
.mk #sprog{margin-left:10px}
.mk .hlw{border:1px solid var(--border);border-radius:10px;padding:8px;margin:8px 0}.mk .hlbox{position:relative;display:inline-block;max-width:340px;cursor:crosshair;touch-action:none;line-height:0}
.mk .hlbox img{max-width:100%;display:block;border-radius:6px;user-select:none}.mk .hlr{position:absolute;border:3px solid #f5a524;border-radius:6px;box-shadow:0 0 0 2px rgba(0,0,0,.45);pointer-events:none}
.mk .ashot{display:flex;gap:8px;align-items:center;margin:4px 0;color:var(--text)}.mk .ashot img{width:80px;height:56px;object-fit:cover;border-radius:6px}.mk .sl .thumb{height:96px;border-radius:8px;margin:6px 10px 6px 0;vertical-align:middle}
</style>"""


def panel():
    opt = lambda d: "".join("<option value='%s'>%s</option>" % (k, v) for k, v in d.items())
    return (
        "<div class='card' id='story' style='display:none'><h3>Story and screenshot slides</h3>"
        "<details open><summary>Screenshot library for this product</summary>" + shots.widget("").replace("class='shotbox'", "class='shotbox' id='cshot'") + "</details>"
        "<div id='sbriefwrap'><label>Describe the story you want (Hindi, English or Hinglish, any length)</label>"
        "<textarea id='sbrief' rows='7' style='font-size:16px' placeholder='Write the story in your own words'></textarea>"
        "<div style='margin:6px 0'><button type='button' class='grey' onclick='loadExample()'>Load example</button></div>"
        "<div class='row'><div><label>Dialogue language</label><select id='slang'><option>Hindi</option><option>Hinglish</option><option>English</option></select></div>"
        "<div><label>Number of slides (3-8)</label><input id='snum' type='number' min='3' max='8' value='5'></div></div>"
        "<div class='row'><div><label>Illustrations</label><select id='simg' onchange='imgStatus()'><option value='ai'>AI illustrations (free, Cloudflare)</option>"
        "<option value='svg'>Drawn parts only (no AI)</option></select></div>"
        "<div><label>&nbsp;</label><label style='display:flex;gap:8px;align-items:center;color:var(--text)'><input type='checkbox' id='shq' onchange='imgStatus()'> High quality (Gemini, paid)</label></div></div>"
        "<div id='imgst' class='note' style='margin:4px 0'></div>"
        "<div class='note'>Product, tone and call-to-action are taken from the fields above. The AI never draws text: Hindi lines are added by the vault.</div>"
        "<div style='margin-top:12px'><button type='button' id='swrite' onclick='writeScript()'>Write script</button><span id='sprog' class='note' role='status'></span></div></div>"
        "<div id='sblankwrap' style='display:none;margin-top:10px'><div class='note'>Build a carousel from your screenshots: start with one slide, add more, then Render.</div>"
        "<button type='button' id='sblank' onclick='startBlank()'>Start screenshot slides</button><span id='sprog2'></span></div>"
        "<div id='sedit' style='display:none;margin-top:16px'><h3>Script <span class='note'>(edit anything, then Render)</span></h3>"
        "<label>Title</label><input id='stitle'><label>Caption</label><textarea id='scap' rows='3'></textarea>"
        "<label>Hashtags <span class='note'>(suggested, popularity not verified)</span></label><textarea id='shash' rows='2'></textarea>"
        "<label>Alt-text description</label><textarea id='salt' rows='2'></textarea><div id='slides'></div>"
        "<div style='margin-top:10px'><button type='button' class='grey' onclick='addSlide(SCRIPT.slides.length-1)'>Add cartoon scene</button> "
        "<button type='button' class='grey' onclick='addSlide(SCRIPT.slides.length-1,\"shot\")'>Add screenshot slide</button> "
        "<button type='button' class='grey' onclick='toggleAddShots()'>Add screenshots to story</button> "
        "<button type='button' class='grey' onclick='writeScript()'>Regenerate whole script</button> "
        "<button type='button' id='srender' onclick='renderStory()'>Render</button></div>"
        "<div id='addshots' class='card' style='display:none;margin-top:10px'><b>Choose 1 to 6 screenshots</b><div id='addlist'></div>"
        "<button type='button' onclick='runAddShots()'>Insert with AI</button> <span class='note'>The AI picks the places, layouts and devices and writes short captions. You can change everything.</span></div>"
        "<div id='tipx'></div><div id='provs' class='note' style='margin-top:10px'></div></div></div>"
        "<div class='lb' id='lb' hidden><button class='x' type='button' onclick='lbClose()'>Close</button>"
        "<button class='nav' type='button' onclick='lbStep(-1)' aria-label='Previous'>&lt;</button><img id='lbimg' alt=''>"
        "<button class='nav' type='button' onclick='lbStep(1)' aria-label='Next'>&gt;</button></div>")


JS = r"""
<script>
var SCRIPT=null,OPT=__OPT__,EXAMPLE=__EX__,LB={list:[],i:0};
function opts(d,sel){return Object.keys(d).map(function(k){return "<option value='"+k+"'"+(k==sel?" selected":"")+">"+d[k]+"</option>";}).join('');}
function arr(a,sel){return a.map(function(k){return "<option"+(k==sel?" selected":"")+">"+k+"</option>";}).join('');}
function isCartoon(){return $('type').value=='cartoon';}
function toggleType(){var t=$('type').value,c=t=='cartoon',k=t=='carousel';$('story').style.display=(c||k)?'block':'none';$('sbriefwrap').style.display=c?'block':'none';$('sblankwrap').style.display=k?'block':'none';$('gen').style.display=c?'none':'inline-block';['topic','template'].forEach(function(i){$(i).parentNode.style.display=c?'none':'block';});}
function loadExample(){$('sbrief').value=EXAMPLE;}
function poll(id,stage,done){var t=setInterval(function(){fetch('/api/marketing/story/job/'+id).then(function(r){return r.json()}).then(function(j){
  if(!j.ok){clearInterval(t);stage(j.error,true);return;}
  if(j.state=='running'){stage(j.stage);return;}clearInterval(t);
  if(j.state=='error'){stage(j.error,true);}else{done(j.result);}}).catch(function(){});},1500);}
function imgStatus(){fetch('/api/marketing/story/imgstatus').then(function(r){return r.json()}).then(function(j){var m=[];
  if($('simg').value=='ai'&&!j.cloudflare)m.push(j.cloudflare_msg+' Until then slides use the drawn parts.');
  if($('shq').checked&&!j.gemini)m.push(j.gemini_msg+' Falls back to the free provider.');
  var u=Object.keys(j.used||{}).map(function(k){return k+' '+j.used[k];}).join(', ');m.push('Images made today: '+(u||'none'));$('imgst').textContent=m.join(' ');}).catch(function(){});}
function regenImage(i){sync();busy(true);prog('drawing picture for slide '+(i+1));post('/api/marketing/story/image',payload({script:SCRIPT,index:i})).then(function(j){if(!j.ok){prog(j.error,true);return;}
  poll(j.job,prog,function(r){SCRIPT.slides[r.index]=r.slide;busy(false);prog('Picture for slide '+(r.index+1)+' made by '+r.slide.provider+(r.slide.img_note?' ('+r.slide.img_note+')':'')+'.');drawSlides();imgStatus();});});}
function busy(on){['swrite','srender'].forEach(function(i){var b=$(i);if(b)b.disabled=on;});}
function prog(t,bad){var p=$('sprog');p.textContent=t;p.style.color=bad?'var(--bad)':'var(--muted)';if(bad)busy(false);}
function payload(extra){var o={brand_id:$('brand').value,product_id:$('product').value,tone:$('tone').value,cta:$('cta').value,language:$('slang').value,imgmode:$('simg').value,hq:$('shq').checked,slides:$('snum').value,brief:$('sbrief').value};
 for(var k in extra)o[k]=extra[k];return o;}
function post(u,o){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)}).then(function(r){return r.json()});}
function writeScript(){if(!$('sbrief').value.trim()){prog('Describe the story first.',true);return;}busy(true);prog('writing script');
 post('/api/marketing/story/script',payload({})).then(function(j){if(!j.ok){prog(j.error,true);return;}
  poll(j.job,prog,function(s){SCRIPT=s;busy(false);prog('Script ready. Check every line, then press Render.');showScript();});});}
function showScript(){$('sedit').style.display='block';$('stitle').value=SCRIPT.title||'';$('scap').value=SCRIPT.caption||'';$('shash').value=SCRIPT.hashtags||'';$('salt').value=SCRIPT.alt||'';drawSlides();}
function sync(){SCRIPT.title=$('stitle').value;SCRIPT.caption=$('scap').value;SCRIPT.hashtags=$('shash').value;SCRIPT.alt=$('salt').value;}
var SHOTS=[],PENDING=null,CHKT=null,RENDERED=false;
function shotInfo(id){return (SHOTS||[]).filter(function(x){return x.id==id})[0]||null;}
function hlStyle(h){return h?"left:"+(h.x*100)+"%;top:"+(h.y*100)+"%;width:"+(h.w*100)+"%;height:"+(h.h*100)+"%":"display:none";}
function sceneBody(s,i){
 var chars=s.characters.map(function(c,k){return "<div class='inl'><select onchange='setc("+i+","+k+",\"role\",this.value)'>"+opts(OPT.roles,c.role)+"</select><select onchange='setc("+i+","+k+",\"expression\",this.value)'>"+arr(OPT.expr,c.expression)+"</select>"+
  "<select onchange='setc("+i+","+k+",\"position\",this.value)'>"+arr(OPT.pos,c.position)+"</select><button type='button' class='grey' onclick='delc("+i+","+k+")'>x</button></div>";}).join('');
 var props=Object.keys(OPT.props).map(function(p){return "<label><input type='checkbox' "+(s.props.indexOf(p)>=0?'checked':'')+" onchange='setp("+i+",\""+p+"\",this.checked)'>"+OPT.props[p]+"</label>";}).join('');
 var roles={};s.characters.forEach(function(c){roles[c.role]=OPT.roles[c.role];});
 var dl=s.dialogue.map(function(d,k){return "<div class='inl'><select style='max-width:160px' onchange='setd("+i+","+k+",\"speaker\",this.value)'>"+opts(roles,d.speaker)+"</select>"+
  "<textarea rows='2' style='flex:3' oninput='setd("+i+","+k+",\"text\",this.value)'>"+esc(d.text)+"</textarea><button type='button' class='grey' onclick='deld("+i+","+k+")'>x</button></div>";}).join('');
 return "<div class='note'>"+(s.image?"<img class='thumb' src='/marketing/img/"+s.image+"'>":"")+(s.provider?"Picture: "+s.provider+(s.provider=='svg'?" (drawn parts)":"")+(s.img_note?" - "+esc(s.img_note):""):"Picture: made when you press Render")+"</div>"+
  "<label>Scene</label><select onchange='sets("+i+",\"scene\",this.value)'>"+opts(OPT.scenes,s.scene)+"</select><label>What the scene shows (English, picture only)</label><input value=\""+esc(s.description)+"\" oninput='sets("+i+",\"description\",this.value)'>"+
  "<label>Characters, expression, position</label>"+chars+(s.characters.length<2?"<button type='button' class='grey' onclick='addc("+i+")'>+ character</button>":"")+
  "<label>Props</label><div class='props'>"+props+"</div><label>Dialogue</label>"+dl+(s.dialogue.length<2?"<button type='button' class='grey' onclick='addd("+i+")'>+ line</button>":"")+
  "<label>Narration (optional line below the picture)</label><input value=\""+esc(s.narration)+"\" oninput='setn("+i+",this.value)'>";}
function shotsBody(s,i){
 var dev=s.type=='device',maxn=dev?1:4;
 var sel=(s.shots||[]).map(function(x,k){var inf=shotInfo(x.id);return "<div class='hlw'><div class='hlbox' data-i='"+i+"' data-k='"+k+"'>"+(inf?"<img src='"+inf.url+"' draggable='false'>":"<div class='note'>screenshot missing from the library</div>")+
  "<div class='hlr' style='"+hlStyle(x.hl)+"'></div></div><div class='note'>"+esc(inf?((inf.title?inf.title+' - ':'')+inf.note):'')+"</div>"+
  "<div class='note'>Drag on the picture to draw a highlight box.</div><label style='display:inline-flex;gap:6px;align-items:center;color:var(--text)'><input type='checkbox' "+(x.hl&&x.hl.arrow?'checked':'')+" onchange='hlArrow("+i+","+k+",this.checked)'> Arrow</label> "+
  "<button type='button' class='grey' onclick='hlClear("+i+","+k+")'>Clear highlight</button> <button type='button' class='grey' onclick='shotRemove("+i+","+k+")'>Remove</button></div>";}).join('');
 var libopts=SHOTS.map(function(x){return "<option value='"+x.id+"'>"+esc((x.title||('Screenshot '+x.id))+' - '+x.note.slice(0,50))+"</option>";}).join('');
 var add=s.shots.length<maxn?"<div class='inl'><select id='pick"+i+"'>"+(libopts||"<option value=''>(library is empty)</option>")+"</select><button type='button' class='grey' onclick='shotAdd("+i+")'>Add from library</button><button type='button' class='grey' onclick='uploadFor("+i+")'>Upload new...</button></div>":"";
 var opt="";
 if(dev){
  var c0=s.characters[0],has=s.dialogue.length>0;
  opt="<label>Device</label><select onchange='sets("+i+",\"device\",this.value)'>"+opts(OPT.devices,s.device)+"</select><label>Background</label><select onchange='sets("+i+",\"bg\",this.value);drawSlides()'>"+opts(OPT.bgs,s.bg)+"</select>"+
   (s.bg=='ai'?"<div class='note'>"+(s.image?"<img class='thumb' src='/marketing/img/"+s.image+"'>":"")+(s.provider?"Background: "+s.provider+(s.img_note?" - "+esc(s.img_note):""):"Empty shop background: made when you press Render (brand gradient if it fails)")+"</div>":"")+
   "<label style='display:flex;gap:6px;align-items:center;color:var(--text)'><input type='checkbox' "+(has?'checked':'')+" onchange='devBubble("+i+",this.checked)'> Speech bubble from a character (shown under the picture)</label>"+
   (has?"<div class='inl'><select onchange='setc("+i+",0,\"role\",this.value);sets("+i+",\"dialogue\",[{speaker:this.value,text:SCRIPT.slides["+i+"].dialogue[0].text}])'>"+opts(OPT.roles,c0.role)+"</select><select onchange='setc("+i+",0,\"position\",this.value)'>"+arr(OPT.pos,c0.position)+"</select>"+
   "<input style='flex:3' value=\""+esc(s.dialogue[0].text)+"\" oninput='setd("+i+",0,\"text\",this.value)'></div>":"");
 }else{
  opt="<label>Layout</label><div class='inl'><select onchange='sets("+i+",\"layout\",this.value)'>"+opts(OPT.layouts,s.layout)+"</select><button type='button' class='grey' onclick='autoLayout("+i+");drawSlides()'>Auto layout</button></div>";
 }
 return "<label>Screenshots ("+s.shots.length+" of "+maxn+")</label>"+sel+add+opt+"<label>Caption (shown under the picture, Hindi, English or Hinglish)</label><textarea rows='2' oninput='setn("+i+",this.value)'>"+esc(s.narration)+"</textarea>";}
function drawSlides(){$('slides').innerHTML=SCRIPT.slides.map(function(s,i){
 var t=s.type||'scene',n=SCRIPT.slides.length;
 var btns=(t=='scene'?"<button type='button' class='grey' onclick='regenImage("+i+")'>Regenerate image</button> <button type='button' class='grey' onclick='regenSlide("+i+")'>Regenerate slide</button> ":
   "<button type='button' class='grey' onclick='regenCaption("+i+")'>Regenerate caption</button> "+(t=='device'&&s.bg=='ai'?"<button type='button' class='grey' onclick='regenImage("+i+")'>Regenerate image</button> ":""))+
  "<button type='button' class='grey' onclick='moveSlide("+i+",-1)' "+(i?'':'disabled')+" aria-label='Move up'>&uarr;</button> <button type='button' class='grey' onclick='moveSlide("+i+",1)' "+(i<n-1?'':'disabled')+" aria-label='Move down'>&darr;</button> "+
  "<button type='button' class='grey' onclick='addSlide("+i+")'>Add below</button> <button type='button' class='grey' onclick='delSlide("+i+")'>Remove</button>";
 return "<div class='sl'><h4><span>Slide "+(i+1)+(s.is_cta?" (call to action)":"")+"</span><span>"+btns+"</span></h4>"+(s.claim_note?"<div class='warn'>"+esc(s.claim_note)+"</div>":"")+
  "<div class='warn' id='tn"+i+"' style='display:none'></div><label>Slide type</label><select onchange='setType("+i+",this.value)'>"+opts(OPT.types,t)+"</select>"+
  (t=='scene'?sceneBody(s,i):shotsBody(s,i))+"</div>";}).join('');bindHl();chk();}
function bindHl(){document.querySelectorAll('.hlbox').forEach(function(el){var i=+el.dataset.i,k=+el.dataset.k;
 el.onpointerdown=function(e){e.preventDefault();var r=el.getBoundingClientRect(),cl=function(v){return Math.max(0,Math.min(1,v));},x0=cl((e.clientX-r.left)/r.width),y0=cl((e.clientY-r.top)/r.height);el.setPointerCapture(e.pointerId);
  var mv=function(ev){var x1=cl((ev.clientX-r.left)/r.width),y1=cl((ev.clientY-r.top)/r.height),old=SCRIPT.slides[i].shots[k].hl,b={x:Math.min(x0,x1),y:Math.min(y0,y1),w:Math.abs(x1-x0),h:Math.abs(y1-y0),arrow:!!(old&&old.arrow)};
   SCRIPT.slides[i].shots[k].hl=b;el.querySelector('.hlr').setAttribute('style',hlStyle(b));};
  var up=function(){el.removeEventListener('pointermove',mv);el.removeEventListener('pointerup',up);var h=SCRIPT.slides[i].shots[k].hl;if(h&&(h.w<0.02||h.h<0.02)){SCRIPT.slides[i].shots[k].hl=null;el.querySelector('.hlr').setAttribute('style',hlStyle(null));}};
  el.addEventListener('pointermove',mv);el.addEventListener('pointerup',up);};});}
function hlArrow(i,k,on){var x=SCRIPT.slides[i].shots[k];if(x.hl)x.hl.arrow=on;else if(on){x.hl=null;}}
function hlClear(i,k){SCRIPT.slides[i].shots[k].hl=null;drawSlides();}
function autoLayout(i){var s=SCRIPT.slides[i],n=s.shots.length;if(n<=1){s.layout='single';return;}
 if(n==2){var w=s.shots.every(function(x){var f=shotInfo(x.id);return f&&f.w&&f.h&&f.w/f.h>=1.3;});s.layout=w?'stack':'2up';}else s.layout=n==4?'grid':'stack';}
function shotAdd(i){var v=$('pick'+i).value,s=SCRIPT.slides[i],mx=s.type=='device'?1:4;if(!v||s.shots.length>=mx||s.shots.some(function(x){return x.id==v;}))return;s.shots.push({id:+v,hl:null});autoLayout(i);drawSlides();}
function shotRemove(i,k){var s=SCRIPT.slides[i];s.shots.splice(k,1);autoLayout(i);drawSlides();}
function uploadFor(i){PENDING=i;var b=$('cshot');b.scrollIntoView({behavior:'smooth'});sbMsg(b,'Add the screenshots here, tick the confirmation and Save: they will be added to slide '+(i+1)+'.');}
function devBubble(i,on){var s=SCRIPT.slides[i];s.dialogue=on?[{speaker:s.characters[0].role,text:''}]:[];if(on&&s.characters[0].position=='center')s.characters[0].position='left';drawSlides();}
function setType(i,t){var s=SCRIPT.slides[i];s.type=t;if(t=='device')s.shots=s.shots.slice(0,1);if(t=='shot'){s.dialogue=[];autoLayout(i);}drawSlides();}
function setn(i,v){SCRIPT.slides[i].narration=v;chk();}
function moveSlide(i,d){var a=SCRIPT.slides,j=i+d;if(j<0||j>=a.length)return;var t=a[i];a[i]=a[j];a[j]=t;drawSlides();}
function regenCaption(i){sync();busy(true);prog('writing caption for slide '+(i+1));post('/api/marketing/story/caption',payload({script:SCRIPT,index:i})).then(function(j){if(!j.ok){prog(j.error,true);return;}
  poll(j.job,prog,function(r){SCRIPT.slides[r.index]=r.slide;busy(false);prog('Caption for slide '+(r.index+1)+' rewritten.');drawSlides();});});}
function chk(){clearTimeout(CHKT);CHKT=setTimeout(function(){if(!SCRIPT)return;post('/api/marketing/story/check',{script:SCRIPT,cta:$('cta').value}).then(function(j){if(!j.ok)return;
  j.notes.forEach(function(t,i){var e=$('tn'+i);if(e){e.textContent=t;e.style.display=t?'block':'none';}});});},600);}
function toggleAddShots(){var p=$('addshots');p.style.display=p.style.display=='none'?'block':'none';drawAddList();}
function drawAddList(){var e=$('addlist');if(!e)return;e.innerHTML=SHOTS.map(function(x){return "<label class='ashot'><input type='checkbox' value='"+x.id+"'><img src='"+x.url+"'><span>"+esc(x.title||('Screenshot '+x.id))+"<br><small>"+esc(x.note)+"</small></span></label>";}).join('')||"<span class='note'>No screenshots in the library for this product yet.</span>";}
function runAddShots(){sync();var ids=Array.prototype.slice.call(document.querySelectorAll('#addlist input:checked')).map(function(x){return +x.value;});
 if(!ids.length||ids.length>6){prog('Choose 1 to 6 screenshots.',true);return;}busy(true);prog('placing screenshots');
 post('/api/marketing/story/addshots',payload({script:SCRIPT,shot_ids:ids})).then(function(j){if(!j.ok){prog(j.error,true);return;}
  poll(j.job,prog,function(r){SCRIPT.slides=r.slides;busy(false);prog('Screenshot slides added. Check the order and the captions; everything is editable.');$('addshots').style.display='none';drawSlides();});});}
function startBlank(){busy(true);prog('starting');post('/api/marketing/story/blank',payload({})).then(function(j){busy(false);if(!j.ok){prog(j.error,true);return;}SCRIPT=j.script;prog('Choose screenshots for each slide, then press Render.');showScript();});}
window.onShotsChanged=function(list,newIds){SHOTS=list;if(PENDING!==null&&newIds.length&&SCRIPT&&SCRIPT.slides[PENDING]){var s=SCRIPT.slides[PENDING],mx=s.type=='device'?1:4;
  newIds.forEach(function(id){if(s.shots.length<mx&&!s.shots.some(function(x){return x.id==id;}))s.shots.push({id:id,hl:null});});autoLayout(PENDING);PENDING=null;}
 if(SCRIPT)drawSlides();drawAddList();};
function shotProduct(){sbSetPid($('cshot'),$('product').value);}
$('product').addEventListener('change',shotProduct);$('brand').addEventListener('change',function(){setTimeout(shotProduct,0);});setTimeout(shotProduct,0);
function isCartoonSave(){var t=$('type').value;return t=='cartoon'||(t=='carousel'&&RENDERED&&!!SCRIPT);}
var _gen0=gen;gen=function(){RENDERED=false;_gen0();};
function sets(i,k,v){SCRIPT.slides[i][k]=v;}
function setc(i,k,f,v){SCRIPT.slides[i].characters[k][f]=v;if(f=='role')drawSlides();}
function delc(i,k){var s=SCRIPT.slides[i];if(s.characters.length>1){s.characters.splice(k,1);drawSlides();}}
function addc(i){var s=SCRIPT.slides[i];s.characters.push({role:'customer',expression:'neutral',position:s.characters[0].position=='left'?'right':'left'});drawSlides();}
function setp(i,p,on){var a=SCRIPT.slides[i].props,x=a.indexOf(p);if(on&&x<0&&a.length<4)a.push(p);else if(on&&x<0){drawSlides();}if(!on&&x>=0)a.splice(x,1);}
function setd(i,k,f,v){SCRIPT.slides[i].dialogue[k][f]=v;}
function deld(i,k){SCRIPT.slides[i].dialogue.splice(k,1);drawSlides();}
function addd(i){var s=SCRIPT.slides[i];s.dialogue.push({speaker:s.characters[0].role,text:''});drawSlides();}
function addSlide(i,t){if(SCRIPT.slides.length>=10){prog('At most 10 slides.',true);return;}t=t||'scene';var n={scene:'shop_counter',description:'',characters:[{role:'shopkeeper',expression:'neutral',position:'center'}],props:[],dialogue:t=='scene'?[{speaker:'shopkeeper',text:''}]:[],narration:'',claim_note:'',type:t,shots:[],layout:'single',device:'tablet',bg:'gradient'};SCRIPT.slides.splice(i+1,0,n);drawSlides();}
function delSlide(i){if(SCRIPT.slides.length<=1){return;}SCRIPT.slides.splice(i,1);drawSlides();}
function regenSlide(i){sync();busy(true);prog('rewriting slide '+(i+1));post('/api/marketing/story/slide',payload({script:SCRIPT,index:i})).then(function(j){if(!j.ok){prog(j.error,true);return;}
  poll(j.job,prog,function(r){SCRIPT.slides[r.index]=r.slide;busy(false);prog('Slide '+(r.index+1)+' rewritten.');drawSlides();});});}
function renderStory(){sync();if(!SCRIPT){return;}busy(true);prog('starting render');
 post('/api/marketing/story/render',payload({script:SCRIPT})).then(function(j){if(!j.ok){prog(j.error,true);return;}
  poll(j.job,prog,function(r){RENDERED=true;busy(false);prog('Rendered '+r.images.length+' slides. Click a picture to enlarge.'+(r.added_cta?' A call-to-action slide was added.':''));TOKEN=r.token;
   if(r.slides){SCRIPT.slides=r.slides;drawSlides();}$('provs').textContent='Pictures by slide: '+(r.slides||[]).map(function(s,k){return (k+1)+' '+(s.provider||'svg')+(s.provider=='svg'&&s.img_note?' ('+s.img_note+')':'');}).join(' | ');imgStatus();LAST={spec:SCRIPT,tiers:SCRIPT.tiers||{}};
   $('result').style.display='block';$('imgs').innerHTML=r.images.map(function(u){return "<img class='pv' style='max-width:200px;margin:0 10px 10px 0' src='"+u+"?t="+Date.now()+"'>"}).join('');
   $('caption').value=SCRIPT.caption||'';$('hashtags').value=SCRIPT.hashtags||'';$('alt').value=SCRIPT.alt||'';$('notes').textContent='Hashtags are suggested; popularity is not verified.';drawTags();
   $('result').scrollIntoView({behavior:'smooth'});});});}
function lbOpen(i){LB.list=Array.prototype.slice.call(document.querySelectorAll('#imgs img')).map(function(x){return x.getAttribute('src').split('?')[0];});LB.i=i;$('lbimg').src=LB.list[i]+'?t='+Date.now();$('lb').hidden=false;}
function lbClose(){$('lb').hidden=true;$('lbimg').src='';}
function lbStep(d){if(!LB.list.length)return;LB.i=(LB.i+d+LB.list.length)%LB.list.length;$('lbimg').src=LB.list[LB.i]+'?t='+Date.now();}
$('imgs').addEventListener('click',function(e){var im=e.target.closest('img');if(!im)return;lbOpen(Array.prototype.indexOf.call($('imgs').querySelectorAll('img'),im));});
$('lb').addEventListener('click',function(e){if(e.target.id=='lb')lbClose();});
document.addEventListener('keydown',function(e){if($('lb').hidden)return;if(e.key=='Escape')lbClose();else if(e.key=='ArrowRight')lbStep(1);else if(e.key=='ArrowLeft')lbStep(-1);});
window.addEventListener('pageshow',function(){lbClose();});
$('type').addEventListener('change',toggleType);toggleType();imgStatus();
</script>"""


def js():
    opt = {"types": shotslides.TYPES, "layouts": shotslides.LAYOUTS, "devices": shotslides.DEVICES, "bgs": shotslides.BGS, "roles": cartoon.ROLES, "scenes": cartoon.SCENES, "props": cartoon.PROPS, "expr": cartoon.EXPRESSIONS, "pos": cartoon.POSITIONS}
    return JS.replace("__OPT__", json.dumps(opt)).replace("__EX__", json.dumps(EXAMPLE))
