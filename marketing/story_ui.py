"""HTML/JS for the Cartoon story panel on the Create page, plus the lightbox used by every preview."""
import json
from . import cartoon

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
</style>"""


def panel():
    opt = lambda d: "".join("<option value='%s'>%s</option>" % (k, v) for k, v in d.items())
    return (
        "<div class='card' id='story' style='display:none'><h3>Cartoon story</h3>"
        "<label>Describe the story you want (Hindi, English or Hinglish, any length)</label>"
        "<textarea id='sbrief' rows='7' style='font-size:16px' placeholder='Write the story in your own words'></textarea>"
        "<div style='margin:6px 0'><button type='button' class='grey' onclick='loadExample()'>Load example</button></div>"
        "<div class='row'><div><label>Dialogue language</label><select id='slang'><option>Hinglish</option><option>Hindi</option><option>English</option></select></div>"
        "<div><label>Number of slides (3-8)</label><input id='snum' type='number' min='3' max='8' value='5'></div></div>"
        "<div class='note'>Product, tone and call-to-action are taken from the fields above.</div>"
        "<div style='margin-top:12px'><button type='button' id='swrite' onclick='writeScript()'>Write script</button><span id='sprog' class='note' role='status'></span></div>"
        "<div id='sedit' style='display:none;margin-top:16px'><h3>Script <span class='note'>(edit anything, then Render)</span></h3>"
        "<label>Title</label><input id='stitle'><label>Caption</label><textarea id='scap' rows='3'></textarea>"
        "<label>Hashtags <span class='note'>(suggested, popularity not verified)</span></label><textarea id='shash' rows='2'></textarea>"
        "<label>Alt-text description</label><textarea id='salt' rows='2'></textarea><div id='slides'></div>"
        "<div style='margin-top:10px'><button type='button' class='grey' onclick='addSlide(SCRIPT.slides.length-1)'>Add slide at end</button> "
        "<button type='button' class='grey' onclick='writeScript()'>Regenerate whole script</button> "
        "<button type='button' id='srender' onclick='renderStory()'>Render</button></div></div></div>"
        "<div class='lb' id='lb' hidden><button class='x' type='button' onclick='lbClose()'>Close</button>"
        "<button class='nav' type='button' onclick='lbStep(-1)' aria-label='Previous'>&lt;</button><img id='lbimg' alt=''>"
        "<button class='nav' type='button' onclick='lbStep(1)' aria-label='Next'>&gt;</button></div>")


JS = r"""
<script>
var SCRIPT=null,OPT=__OPT__,EXAMPLE=__EX__,LB={list:[],i:0};
function opts(d,sel){return Object.keys(d).map(function(k){return "<option value='"+k+"'"+(k==sel?" selected":"")+">"+d[k]+"</option>";}).join('');}
function arr(a,sel){return a.map(function(k){return "<option"+(k==sel?" selected":"")+">"+k+"</option>";}).join('');}
function isCartoon(){return $('type').value=='cartoon';}
function toggleType(){var c=isCartoon();$('story').style.display=c?'block':'none';$('gen').style.display=c?'none':'inline-block';
 ['topic','template'].forEach(function(i){$(i).parentNode.style.display=c?'none':'block';});}
function loadExample(){$('sbrief').value=EXAMPLE;}
function poll(id,stage,done){var t=setInterval(function(){fetch('/api/marketing/story/job/'+id).then(function(r){return r.json()}).then(function(j){
  if(!j.ok){clearInterval(t);stage(j.error,true);return;}
  if(j.state=='running'){stage(j.stage);return;}clearInterval(t);
  if(j.state=='error'){stage(j.error,true);}else{done(j.result);}}).catch(function(){});},1500);}
function busy(on){['swrite','srender'].forEach(function(i){var b=$(i);if(b)b.disabled=on;});}
function prog(t,bad){var p=$('sprog');p.textContent=t;p.style.color=bad?'var(--bad)':'var(--muted)';if(bad)busy(false);}
function payload(extra){var o={brand_id:$('brand').value,product_id:$('product').value,tone:$('tone').value,cta:$('cta').value,language:$('slang').value,slides:$('snum').value,brief:$('sbrief').value};
 for(var k in extra)o[k]=extra[k];return o;}
function post(u,o){return fetch(u,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(o)}).then(function(r){return r.json()});}
function writeScript(){if(!$('sbrief').value.trim()){prog('Describe the story first.',true);return;}busy(true);prog('writing script');
 post('/api/marketing/story/script',payload({})).then(function(j){if(!j.ok){prog(j.error,true);return;}
  poll(j.job,prog,function(s){SCRIPT=s;busy(false);prog('Script ready. Check every line, then press Render.');showScript();});});}
function showScript(){$('sedit').style.display='block';$('stitle').value=SCRIPT.title||'';$('scap').value=SCRIPT.caption||'';$('shash').value=SCRIPT.hashtags||'';$('salt').value=SCRIPT.alt||'';drawSlides();}
function sync(){SCRIPT.title=$('stitle').value;SCRIPT.caption=$('scap').value;SCRIPT.hashtags=$('shash').value;SCRIPT.alt=$('salt').value;}
function drawSlides(){$('slides').innerHTML=SCRIPT.slides.map(function(s,i){
 var chars=s.characters.map(function(c,k){return "<div class='inl'><select onchange='setc("+i+","+k+",\"role\",this.value)'>"+opts(OPT.roles,c.role)+"</select><select onchange='setc("+i+","+k+",\"expression\",this.value)'>"+arr(OPT.expr,c.expression)+"</select>"+
  "<select onchange='setc("+i+","+k+",\"position\",this.value)'>"+arr(OPT.pos,c.position)+"</select><button type='button' class='grey' onclick='delc("+i+","+k+")'>x</button></div>";}).join('');
 var props=Object.keys(OPT.props).map(function(p){return "<label><input type='checkbox' "+(s.props.indexOf(p)>=0?'checked':'')+" onchange='setp("+i+",\""+p+"\",this.checked)'>"+OPT.props[p]+"</label>";}).join('');
 var roles={};s.characters.forEach(function(c){roles[c.role]=OPT.roles[c.role];});
 var dl=s.dialogue.map(function(d,k){return "<div class='inl'><select style='max-width:160px' onchange='setd("+i+","+k+",\"speaker\",this.value)'>"+opts(roles,d.speaker)+"</select>"+
  "<textarea rows='2' style='flex:3' oninput='setd("+i+","+k+",\"text\",this.value)'>"+esc(d.text)+"</textarea><button type='button' class='grey' onclick='deld("+i+","+k+")'>x</button></div>";}).join('');
 return "<div class='sl'><h4><span>Slide "+(i+1)+"</span><span><button type='button' class='grey' onclick='regenSlide("+i+")'>Regenerate slide</button> <button type='button' class='grey' onclick='addSlide("+i+")'>Add below</button> "+
  "<button type='button' class='grey' onclick='delSlide("+i+")'>Remove</button></span></h4>"+(s.claim_note?"<div class='warn'>"+esc(s.claim_note)+"</div>":"")+
  "<label>Scene</label><select onchange='sets("+i+",\"scene\",this.value)'>"+opts(OPT.scenes,s.scene)+"</select><label>What the scene shows (note)</label><input value=\""+esc(s.description)+"\" oninput='sets("+i+",\"description\",this.value)'>"+
  "<label>Characters, expression, position</label>"+chars+(s.characters.length<2?"<button type='button' class='grey' onclick='addc("+i+")'>+ character</button>":"")+
  "<label>Props</label><div class='props'>"+props+"</div><label>Dialogue</label>"+dl+(s.dialogue.length<2?"<button type='button' class='grey' onclick='addd("+i+")'>+ line</button>":"")+
  "<label>Narration (optional line under the picture)</label><input value=\""+esc(s.narration)+"\" oninput='sets("+i+",\"narration\",this.value)'></div>";}).join('');}
function sets(i,k,v){SCRIPT.slides[i][k]=v;}
function setc(i,k,f,v){SCRIPT.slides[i].characters[k][f]=v;if(f=='role')drawSlides();}
function delc(i,k){var s=SCRIPT.slides[i];if(s.characters.length>1){s.characters.splice(k,1);drawSlides();}}
function addc(i){var s=SCRIPT.slides[i];s.characters.push({role:'customer',expression:'neutral',position:s.characters[0].position=='left'?'right':'left'});drawSlides();}
function setp(i,p,on){var a=SCRIPT.slides[i].props,x=a.indexOf(p);if(on&&x<0&&a.length<4)a.push(p);else if(on&&x<0){drawSlides();}if(!on&&x>=0)a.splice(x,1);}
function setd(i,k,f,v){SCRIPT.slides[i].dialogue[k][f]=v;}
function deld(i,k){SCRIPT.slides[i].dialogue.splice(k,1);drawSlides();}
function addd(i){var s=SCRIPT.slides[i];s.dialogue.push({speaker:s.characters[0].role,text:''});drawSlides();}
function addSlide(i){if(SCRIPT.slides.length>=8){prog('At most 8 slides.',true);return;}SCRIPT.slides.splice(i+1,0,{scene:'shop_counter',description:'',characters:[{role:'shopkeeper',expression:'neutral',position:'center'}],props:[],dialogue:[{speaker:'shopkeeper',text:''}],narration:'',claim_note:''});drawSlides();}
function delSlide(i){if(SCRIPT.slides.length<=1){return;}SCRIPT.slides.splice(i,1);drawSlides();}
function regenSlide(i){sync();busy(true);prog('rewriting slide '+(i+1));post('/api/marketing/story/slide',payload({script:SCRIPT,index:i})).then(function(j){if(!j.ok){prog(j.error,true);return;}
  poll(j.job,prog,function(r){SCRIPT.slides[r.index]=r.slide;busy(false);prog('Slide '+(r.index+1)+' rewritten.');drawSlides();});});}
function renderStory(){sync();if(!SCRIPT){return;}busy(true);prog('starting render');
 post('/api/marketing/story/render',payload({script:SCRIPT})).then(function(j){if(!j.ok){prog(j.error,true);return;}
  poll(j.job,prog,function(r){busy(false);prog('Rendered '+r.images.length+' slides. Click a picture to enlarge.');TOKEN=r.token;LAST={spec:SCRIPT,tiers:SCRIPT.tiers||{}};
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
$('type').addEventListener('change',toggleType);toggleType();
</script>"""


def js():
    opt = {"roles": cartoon.ROLES, "scenes": cartoon.SCENES, "props": cartoon.PROPS, "expr": cartoon.EXPRESSIONS, "pos": cartoon.POSITIONS}
    return JS.replace("__OPT__", json.dumps(opt)).replace("__EX__", json.dumps(EXAMPLE))
