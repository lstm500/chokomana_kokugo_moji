# -*- coding: utf-8 -*-
"""音声入力からA4硬筆PDFを作るアプリ。チャレンジ・採点はありません。"""
from __future__ import annotations
import base64, hashlib, io, json, os, re, sys, tempfile, time, unicodedata, uuid
from pathlib import Path
from urllib.parse import urlsplit
APP_VERSION = "kana-kouhitsu-1.1"
DEFAULT_HUB_URL = "https://chokomana-hkgx4uyxxezjtr2sj35jwj.streamlit.app/"
MAX_WORKSHEET_CHARS = 60
def to_katakana(text: str) -> str:
    return "".join(chr(ord(x) + 96) if "ぁ" <= x <= "ゖ" else x for x in text)

def to_hiragana(text: str) -> str:
    return "".join(chr(ord(x) - 96) if "ァ" <= x <= "ヶ" else x for x in text)

def _safe_https_url(value: str) -> str:
    if not value:
        return ""
    p=urlsplit(value)
    return value.strip().rstrip("/")+"/" if p.scheme=="https" and p.netloc and not p.query and not p.fragment and not p.username else ""

def normalize_worksheet_text(text: str, script: str) -> str:
    """Orthography only; never silently guess a dictated neologism's meaning."""
    if script not in ("hiragana","katakana"):raise ValueError("invalid_script")
    clean=unicodedata.normalize("NFKC",str(text)).replace("\u3000"," ")
    clean="".join(ch for ch in clean if ch.isprintable() and ch not in "\r\n\t")
    clean=re.sub(r"[。．、，!！?？\"'「」『』（）()\[\]【】]", "", clean).strip()
    clean=re.sub(r"\s+","",clean)
    clean = to_hiragana(clean) if script=="hiragana" else to_katakana(clean)
    if not clean or len(clean)>MAX_WORKSHEET_CHARS:raise ValueError("invalid_length")
    allowed=r"[ぁ-ゖー]+" if script=="hiragana" else r"[ァ-ヶー]+"
    if not re.fullmatch(allowed,clean):raise ValueError("non_kana_character")
    return clean

def normalize_spoken_word(text:str,script:str,word_mode:str="auto") -> dict:
    """Conservative kana transcription; never guesses an unusual name's semantics."""
    if word_mode not in ("auto","usual","playful") or script not in ("hiragana","katakana"):
        raise ValueError("invalid_mode")
    original=unicodedata.normalize("NFKC",str(text))
    original="".join(ch for ch in original if ch.isprintable())
    if len(original)>MAX_WORKSHEET_CHARS:raise ValueError("invalid_length")
    converted_kanji=False
    if re.search(r"[\u3400-\u9fff\uf900-\ufaff]",original):
        import pykakasi  # deferred; only for kana preview with kanji recognition results
        pieces=pykakasi.kakasi().convert(original)
        original="".join(item["hira"] if script=="hiragana" else item["kana"] for item in pieces)
        converted_kanji=True
    normalized=normalize_worksheet_text(original,script)
    return {"text":normalized,"was_transliterated":converted_kanji,
       "semantic_correction":"not_claimed", "word_mode":word_mode,
       "message":("よみを じどうで なおしました。いんさつ まえに かくにんしてね。" if converted_kanji
         else "ことばを かくにんしてね。ふたしかな ことばは かってに なおしません。")}

def worksheet_plan(word: str, orientation: str) -> dict:
    if orientation not in ("horizontal","vertical"):raise ValueError("invalid_orientation")
    if not 1<=len(word)<=MAX_WORKSHEET_CHARS:raise ValueError("invalid_length")
    repeats=3 if len(word)<=3 else (2 if len(word)<=9 else 1)
    max_per_line=8 if orientation=="horizontal" else 11
    chunks=[word[i:i+max_per_line] for i in range(0,len(word),max_per_line)]
    return {"word":word,"orientation":orientation,"repeats":repeats,"chunks":chunks,"max_per_line":max_per_line}

def _register_pdf_font() -> str:
    """IPAexG ships as a pip dependency; no user's font file is redistributed."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    candidates=[]
    env=os.environ.get("KANA_FONT_PATH","")
    if env:candidates.append(Path(env))
    try:
        import importlib.util
        spec=importlib.util.find_spec("japanize_matplotlib")
        if spec and spec.submodule_search_locations:
            d=Path(next(iter(spec.submodule_search_locations)))
            candidates.extend((d/"fonts"/"ipaexg.ttf",d/"fonts"/"ipag.ttf"))
    except Exception:pass
    candidates.extend([Path("/usr/share/fonts/truetype/fonts-japanese-gothic.ttf"),Path("/usr/share/fonts/truetype/takao-gothic/TakaoGothic.ttf")])
    for path in candidates:
        if path.is_file():
            try:
                if "KanaJP" not in pdfmetrics.getRegisteredFontNames():pdfmetrics.registerFont(TTFont("KanaJP",str(path)))
                return "KanaJP"
            except Exception:continue
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    return "HeiseiKakuGo-W5"

def generate_worksheet_pdf(text: str, script: str, orientation: str) -> bytes:
    """A4: pale glyphs, thin square grid and writing guides, zero extra ink-heavy elements."""
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.colors import Color
    from reportlab.lib.units import mm
    word=normalize_worksheet_text(text,script)
    plan=worksheet_plan(word,orientation)
    font=_register_pdf_font()
    buf=io.BytesIO()
    pdf=canvas.Canvas(buf,pagesize=A4,pageCompression=1)
    pdf.setTitle("こうひつ れんしゅう")
    width,height=A4
    margin=14*mm
    cell=min(23*mm,(width-2*margin)/8.4)
    row_step=cell*1.32
    grid=Color(.77,.77,.77)
    guide=Color(.91,.91,.91)
    trace=Color(.70,.70,.70)

    def box(x:float,y:float,ch:str | None):
        pdf.setStrokeColor(grid);pdf.setLineWidth(.38)
        pdf.rect(x,y,cell,cell,stroke=1,fill=0)
        pdf.setStrokeColor(guide);pdf.setLineWidth(.3);pdf.setDash(1.7,2.5)
        pdf.line(x+cell/2,y+2,x+cell/2,y+cell-2)
        pdf.line(x+2,y+cell/2,x+cell-2,y+cell/2)
        pdf.setDash()
        if ch:
            pdf.setFillColor(trace);pdf.setFont(font,cell*.64)
            if orientation=="vertical" and ch=="ー":
                pdf.saveState();pdf.translate(x+cell/2,y+cell/2);pdf.rotate(-90)
                pdf.drawCentredString(0,-cell*.29,ch);pdf.restoreState()
            else:pdf.drawCentredString(x+cell/2,y+cell*.21,ch)

    if orientation=="horizontal":
        y=height-margin-cell
        for repeat in range(plan["repeats"]):
            for chunk in plan["chunks"]:
                if y < margin+cell*1.35:
                    pdf.showPage();y=height-margin-cell
                # A faint tracing row followed by an empty row for independent writing.
                for i,char in enumerate(chunk):box(margin+i*cell,y,char)
                y-=row_step
                for i in range(len(chunk)):box(margin+i*cell,y,None)
                y-=row_step*1.3
    else:
        # Japanese columns from right to left, trace column then blank column.
        x=width-margin-cell
        for repeat in range(plan["repeats"]):
            for chunk in plan["chunks"]:
                if x < margin+cell:
                    pdf.showPage();x=width-margin-cell
                for i,char in enumerate(chunk):box(x,height-margin-(i+1)*cell,char)
                x-=row_step
                for i in range(len(chunk)):box(x,height-margin-(i+1)*cell,None)
                x-=row_step*1.30
    pdf.save()
    return buf.getvalue()

def process_message(st, message):
    result={"delivery_id":message.get("delivery_id"),"ok":False,"code":"invalid_input","revision":message.get("revision")}
    try:
        uuid.UUID(str(message.get("delivery_id")))
        if message.get("kind")=="normalize":
            result.update(ok=True,normalized=normalize_spoken_word(message.get("text",""),message.get("script",""),message.get("word_mode","auto")))
        elif message.get("kind")=="pdf":
            pdf=generate_worksheet_pdf(message.get("text",""),message.get("script",""),message.get("orientation",""))
            result.update(ok=True,pdf_base64=base64.b64encode(pdf).decode("ascii"))
    except Exception:
        pass
    return result


HTML_TEMPLATE = r'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>
:root{color-scheme:light;--blue:#315f86;--line:#ded6c9}*{box-sizing:border-box}body{margin:0;background:#f7f5ef;color:#292b2a;font:16px system-ui,-apple-system,'Yu Gothic',sans-serif}main{max-width:940px;margin:auto;padding:16px 16px 36px}header{display:flex;align-items:center;justify-content:space-between;gap:12px}h1{font-size:27px;margin:12px 0 20px}h2{font-size:19px;margin:0 0 20px}button,a{font:inherit;touch-action:manipulation}button,.btn{min-height:46px;padding:10px 14px;border:1px solid var(--line);border-radius:12px;background:#fff;color:#263442;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center}button:focus-visible,a:focus-visible{outline:3px solid #76a7d2;outline-offset:2px}button.primary{background:var(--blue);color:white;border-color:var(--blue);font-weight:700}button[aria-pressed=true]{background:#e5eff6;border:2px solid #467fa8}button:disabled{opacity:.5;cursor:default}.card{background:#fffefa;border:1px solid var(--line);border-radius:19px;padding:22px}.seg{display:flex;gap:8px;margin:8px 0 18px}.seg button{flex:1}.label{font-size:14px;color:#53616a}input{font:26px system-ui;width:100%;padding:14px;border:1px solid #b8c0c7;border-radius:12px;background:#fff;color:#20282e;margin:8px 0 10px}p{font-size:13px;color:#647078;line-height:1.7}.actions{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin:12px 0}.status{font-size:14px;color:#53616a;min-height:22px}.candidates{display:flex;flex-wrap:wrap;gap:6px}.candidates button{font-size:16px;padding:6px 12px}.preview{background:white;aspect-ratio:210/297;width:100%;max-width:410px;margin:14px auto 22px;border:1px solid #ccc;padding:6.66%;overflow:hidden;--cell:clamp(18px,7vw,38px);--group-gap:calc(var(--cell)*.73)}.cells{display:flex}.cell{width:var(--cell);height:var(--cell);position:relative;border:1px solid #ccc;flex:none;display:grid;place-items:center;color:#b3b3b3;font-size:calc(var(--cell)*.64);font-family:serif}.cell:before,.cell:after{content:'';position:absolute;pointer-events:none}.cell:before{height:100%;border-left:1px dashed #e8e8e8;left:50%}.cell:after{width:100%;border-top:1px dashed #e8e8e8;top:50%}.cell span{z-index:1}.trace-group{display:flex;gap:calc(var(--cell)*.32)}.horizontal .trace-group{flex-direction:column;margin-bottom:var(--group-gap)}.vertical{display:flex;flex-direction:row-reverse;gap:var(--group-gap);justify-content:flex-start}.vertical .trace-group{flex-direction:row-reverse}.vertical .cells{flex-direction:column}.vertical .longmark{transform:rotate(90deg)}details{margin-top:18px}summary{cursor:pointer;font-size:14px;color:#53616a;padding:8px 0}[hidden]{display:none!important}.small{font-size:12px}.error{color:#a54232}@media(max-width:480px){main{padding:10px}.card{padding:16px}h1{font-size:24px}.preview{padding:6.66%;--cell:clamp(18px,7.5vw,38px)}header .btn{font-size:13px;padding:8px}}
 .preview.page{position:relative;aspect-ratio:210/297;padding:0;container-type:inline-size;overflow:hidden}.page .cell{font-size:5.55cqw}.preview-pages{width:100%}
</style></head><body><main id="app"><header><span class="small">ちょこまな 学習アプリ</span><a id="hub" class="btn" target="_blank" rel="noopener noreferrer" href="https://chokomana-hkgx4uyxxezjtr2sj35jwj.streamlit.app/">← ちょこまなへ</a></header><h1>ひらがな・カタカナ</h1><section class="card"><h2>こうひつ プリント</h2>
<div class="label">もじ</div><div class="seg" id="scripts"><button data-script="hiragana" aria-pressed="true">ひらがな</button><button data-script="katakana" aria-pressed="false">カタカナ</button></div>
<label class="label" for="word">ことばを いってね</label><input id="word" placeholder="はなすか、もじを いれてね" maxlength="120" autocomplete="off"><div class="actions"><button id="mic" class="primary" aria-pressed="false">マイクで はなす</button><span id="voice" class="status" role="status" aria-live="polite"></span></div><div id="candidates" class="candidates" aria-label="ほかの ききとり こうほ"></div>
<details><summary>ことばの あつかい</summary><div class="seg" id="modes"><button data-mode="auto" aria-pressed="true">じどう</button><button data-mode="usual" aria-pressed="false">ふつう</button><button data-mode="playful" aria-pressed="false">おもしろ</button></div><p>おもしろい ことばは、きこえた おとを だいじにします。ちがう ときは、もじを そのまま なおせます。</p></details>
<div class="label">かきかた</div><div class="seg" id="directions"><button data-dir="horizontal" aria-pressed="true">よこがき</button><button data-dir="vertical" aria-pressed="false">たてがき</button></div>
<div id="info" class="status" role="status" aria-live="polite"></div><div id="previewSection" hidden><div class="label">いんさつ まえの プレビュー</div><div id="preview" class="preview horizontal"></div><p>ことばが ちがう ときは、うえの もじを なおしてね。</p><button id="pdf" class="primary">PDFを ダウンロード</button><div id="pdfStatus" class="status" role="status" aria-live="polite"></div></div>
</section><details><summary>設定・動作ログ</summary><p>認識した言葉や音声はログに保存しません。</p><button id="logs">動作ログを ダウンロード</button></details></main>
<script>
'use strict';
const $=s=>document.querySelector(s),$$=s=>Array.from(document.querySelectorAll(s));
let script='hiragana',orientation='horizontal',mode='auto',revision=0,word='',timer=null,active=null,queue=[],lastReply='',pdfSnapshot=null;
let recognition=null,micWanted=false,run=0,retryTimer=null,watchdog=null,retries=0,gotResult=false,logs=[];
const uuid=()=>crypto.randomUUID();
function diag(type,data={}){logs.push({at:new Date().toISOString(),type,...data});logs=logs.slice(-400);try{localStorage.setItem('kana_kouhitsu_diagnostics_v11',JSON.stringify(logs))}catch{}}
try{logs=JSON.parse(localStorage.getItem('kana_kouhitsu_diagnostics_v11')||'[]');if(!Array.isArray(logs))logs=[]}catch{}
function height(){requestAnimationFrame(()=>parent.postMessage({isStreamlitMessage:true,type:'streamlit:setFrameHeight',height:document.documentElement.scrollHeight},'*'))}
function bridge(kind,fields){const m={delivery_id:uuid(),kind,...fields};if(kind==='normalize')queue=queue.filter(x=>x.kind!=='normalize');queue.push(m);pump()}
function pump(){if(active||!queue.length)return;active=queue.shift();parent.postMessage({isStreamlitMessage:true,type:'streamlit:setComponentValue',value:active},'*')}
function select(group,attr,value){$$(group+' button').forEach(b=>b.setAttribute('aria-pressed',String(b.dataset[attr]===value)))}
function normalize(s){s=String(s).normalize('NFKC').replace(/[。．、，!！?？"'「」『』（）()\[\]【】\s]/g,'');s=Array.from(s).map(x=>script==='hiragana'&&x>='ァ'&&x<='ヶ'?String.fromCharCode(x.charCodeAt(0)-96):script==='katakana'&&x>='ぁ'&&x<='ゖ'?String.fromCharCode(x.charCodeAt(0)+96):x).join('');if(!s||s.length>60||!(script==='hiragana'?/^[ぁ-ゖー]+$/:/^[ァ-ヶー]+$/).test(s))throw Error('ひらがな・カタカナで 1〜60もじ いれてね');return s}
function clearPreview(){word='';$('#previewSection').hidden=true;height()}
function update(){clearTimeout(timer);revision++;const text=$('#word').value;$('#pdfStatus').textContent='';if(!text.trim()){clearPreview();$('#info').textContent='';return}
 if(/[\u3400-\u9fff\uf900-\ufaff]/.test(text)){clearPreview();$('#info').textContent='かなに なおしています…';bridge('normalize',{text,script,word_mode:mode,revision});return}
 try{word=normalize(text);$('#word').value=word;$('#info').textContent='';draw()}catch(e){clearPreview();$('#info').textContent=e.message}}
function draw(){const out=$('#preview');out.textContent='';out.className='preview-pages';out.removeAttribute('style');
 const mm=72/25.4,W=595.275590551,H=841.88976378,margin=14*mm,cell=Math.min(23*mm,(W-2*margin)/8.4),step=cell*1.32;
 const chars=Array.from(word),n=chars.length,count=n<=3?3:n<=9?2:1,max=orientation==='horizontal'?8:11;let page;
 const newPage=()=>{page=document.createElement('div');page.className='preview page';out.append(page)};
 const box=(x,y,ch)=>{const el=document.createElement('div');el.className='cell';Object.assign(el.style,{position:'absolute',left:(x/W*100)+'%',top:((H-y-cell)/H*100)+'%',width:(cell/W*100)+'%',height:(cell/H*100)+'%'});const span=document.createElement('span');span.textContent=ch||'';if(orientation==='vertical'&&ch==='ー')span.className='longmark';el.append(span);page.append(el)};
 newPage();let x=W-margin-cell,y=H-margin-cell;
 for(let repeat=0;repeat<count;repeat++)for(let i=0;i<n;i+=max){const chunk=chars.slice(i,i+max);
 if(orientation==='horizontal'){if(y<margin+cell*1.35){newPage();y=H-margin-cell}chunk.forEach((ch,j)=>box(margin+j*cell,y,ch));y-=step;chunk.forEach((ch,j)=>box(margin+j*cell,y,''));y-=step*1.3}
 else{if(x<margin+cell){newPage();x=W-margin-cell}chunk.forEach((ch,j)=>box(x,H-margin-(j+1)*cell,ch));x-=step;chunk.forEach((ch,j)=>box(x,H-margin-(j+1)*cell,''));x-=step*1.3}}
 $('#previewSection').hidden=false;$('#pdf').disabled=false;height();diag('preview_updated',{direction:orientation,characters:n})}
function download(name,bytes,type){const url=URL.createObjectURL(new Blob([bytes],{type})),a=document.createElement('a');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000)}
function requestPDF(){clearTimeout(timer);if(!word||$('#previewSection').hidden)return;stopMic();pdfSnapshot={text:word,script,orientation};$('#pdf').disabled=true;$('#pdfStatus').textContent='PDFを つくっています…';bridge('pdf',pdfSnapshot)}
function onReply(result){if(!result||!active||result.delivery_id!==active.delivery_id||result.delivery_id===lastReply)return;lastReply=result.delivery_id;const req=active;active=null;
 if(req.kind==='normalize'&&req.revision===revision&&req.text===$('#word').value&&req.script===script){if(result.ok){$('#word').value=result.normalized.text;word=result.normalized.text;$('#info').textContent='よみを かくにんしてね。ちがう ときは うえで なおせます。';draw()}else{$('#info').textContent='よみを へんかんできませんでした。かなで なおしてね。';clearPreview()}}
 if(req.kind==='pdf'){$('#pdf').disabled=false;if(result.ok){const bytes=Uint8Array.from(atob(result.pdf_base64),c=>c.charCodeAt(0));download('こうひつ_'+req.text.slice(0,20)+'.pdf',bytes,'application/pdf');$('#pdfStatus').textContent='PDFを つくりました。A4で いんさつしてね。';diag('pdf_created',{direction:req.orientation})}else{$('#pdfStatus').textContent='PDFを つくれませんでした。もういちど おしてね。';diag('pdf_error')}}pump();height()}
function micUI(text){$('#mic').textContent=micWanted?'マイクを とめる':'マイクで はなす';$('#mic').setAttribute('aria-pressed',String(micWanted));if(text!==undefined)$('#voice').textContent=text;height()}
function detach(){clearTimeout(watchdog);watchdog=null;const r=recognition;recognition=null;if(r){r.onstart=r.onresult=r.onerror=r.onend=r.onspeechstart=r.onspeechend=null;try{r.abort()}catch{}}}
function stopMic(){micWanted=false;run++;clearTimeout(retryTimer);detach();micUI('')}
function retry(token){if(!micWanted||token!==run||document.hidden)return;if(++retries>2){stopMic();micUI('ききとれませんでした。もういちど マイクを おしてね。');return}retryTimer=setTimeout(()=>listen(token),300)}
function listen(token){if(!micWanted||recognition||token!==run)return;const SR=window.SpeechRecognition||window.webkitSpeechRecognition;if(!SR){stopMic();micUI('このブラウザは おんせいに たいおうしていません。もじを いれてね。');return}
 const r=new SR();recognition=r;gotResult=false;r.lang='ja-JP';r.continuous=false;r.interimResults=true;r.maxAlternatives=5;const current=()=>recognition===r&&token===run;
 r.onstart=()=>{if(!current())return;clearTimeout(watchdog);watchdog=setTimeout(()=>{if(current()){detach();retry(token)}},20000);micUI('きいています…');diag('microphone_started')};
 r.onspeechstart=()=>{if(current())micUI('きいています…')};
 r.onresult=e=>{if(!current())return;const finals=[],interims=[];let alternatives=[];for(let i=0;i<e.results.length;i++){const item=e.results[i];if(item.isFinal){finals.push(item[0]?.transcript||'');if(i===e.results.length-1)alternatives=Array.from(item).map(x=>({text:x.transcript,confidence:x.confidence||0}))}else interims.push(item[0]?.transcript||'')}
  if(interims.length){micUI('ききとり：'+finals.join('')+interims.join(''));return}if(!finals.length)return;
  const prefix=finals.slice(0,-1).join('');alternatives=alternatives.map(x=>({...x,text:prefix+x.text}));if(!alternatives.length)alternatives=[{text:finals.join(''),confidence:0}];
  if(mode==='playful'){const kana=alternatives.find(x=>/^[ぁ-ゖァ-ヶー\s。、]+$/.test(x.text));if(kana)alternatives=[kana,...alternatives.filter(x=>x!==kana)]}
  const unique=[...new Set(alternatives.map(x=>x.text.trim()).filter(Boolean))];gotResult=true;$('#word').value=unique[0];$('#candidates').textContent='';for(const text of unique.slice(1,5)){const b=document.createElement('button');b.textContent=text;b.onclick=()=>{$('#word').value=text;$('#candidates').textContent='';update()};$('#candidates').append(b)}
  update();micUI('ききとりました。');diag('microphone_result',{candidates:unique.length,characters:unique[0].length});
  // Finish the browser callback before aborting (Android/Safari lifecycle).
  setTimeout(()=>{if(current()){stopMic();micUI('ききとりました。')}} ,0);
 };
 r.onerror=e=>{if(!current())return;const code=e.error||'unknown';diag('microphone_error',{code});const fatal=['not-allowed','service-not-allowed','audio-capture','language-not-supported'].includes(code);detach();if(fatal){stopMic();micUI(code==='network'?'おんせいの つうしんに しっぱいしました。もういちど マイクを おしてね。':code==='not-allowed'||code==='service-not-allowed'?'ブラウザで マイクを きょかしてね。':'マイクを かくにんしてね。')}else{micUI('もういちど はなしてね。');retry(token)}};
 r.onend=()=>{if(!current())return;const completed=gotResult;detach();if(completed){stopMic();micUI('ききとりました。')}else retry(token)};
 try{r.start();watchdog=setTimeout(()=>{if(current()){detach();retry(token)}},6000)}catch(e){diag('microphone_error',{code:e.name});detach();if(['NotAllowedError','SecurityError'].includes(e.name)){stopMic();micUI('ブラウザで マイクを きょかしてね。')}else retry(token)}
}
$('#mic').onclick=()=>{if(micWanted){stopMic();return}retries=0;micWanted=true;run++;$('#candidates').textContent='';micUI('マイクを じゅんびしています…');listen(run)};
$('#word').addEventListener('input',()=>{if(micWanted)stopMic();$('#candidates').textContent='';clearPreview();clearTimeout(timer);revision++;timer=setTimeout(update,220)});
$$('[data-script]').forEach(b=>b.onclick=()=>{script=b.dataset.script;select('#scripts','script',script);update()});
$$('[data-dir]').forEach(b=>b.onclick=()=>{orientation=b.dataset.dir;select('#directions','dir',orientation);if(word)draw()});
$$('[data-mode]').forEach(b=>b.onclick=()=>{mode=b.dataset.mode;select('#modes','mode',mode);update()});
$('#pdf').onclick=requestPDF;$('#logs').onclick=()=>download('kana_diagnostics.json',JSON.stringify({app_version:'kana-kouhitsu-1.1',entries:logs},null,2),'application/json');
window.addEventListener('message',e=>{if(e.source!==parent||e.data?.type!=='streamlit:render')return;const args=e.data.args||{};if(args.chokomana_home_url?.startsWith('https://'))$('#hub').href=args.chokomana_home_url;onReply(args.result);height()});
window.addEventListener('pagehide',stopMic);document.addEventListener('visibilitychange',()=>{if(document.hidden)stopMic()});window.addEventListener('error',()=>diag('js_error'));new ResizeObserver(height).observe($('#app'));$$('details').forEach(x=>x.addEventListener('toggle',height));
parent.postMessage({isStreamlitMessage:true,type:'streamlit:componentReady',apiVersion:1},'*');diag('startup');height();
</script></body></html>
'''

def frontend_html():
    return HTML_TEMPLATE

def _run_streamlit():
    run_t0=time.perf_counter()
    import streamlit as st
    import streamlit.components.v1 as components
    st.set_page_config(page_title="ひらがな・カタカナ",page_icon="✏️",layout="centered",initial_sidebar_state="collapsed")
    st.markdown("""<style>
        #MainMenu,header[data-testid='stHeader'],footer{display:none!important}
        [data-testid='stMainBlockContainer']{padding:0!important;max-width:none!important}
        [data-testid='stMain']{background:#f7f5ef}
        [data-testid='stVerticalBlock']{gap:0!important}
        iframe[title^='chokomana_kana']{border:none!important}
     .preview.page{position:relative;aspect-ratio:210/297;padding:0;container-type:inline-size;overflow:hidden}.page .cell{font-size:5.55cqw}.preview-pages{width:100%}
</style>""",unsafe_allow_html=True)
    # Stable single HTML component; Python side does no Supabase I/O on the startup path.
    @st.cache_resource(show_spinner=False)
    def get_component():
        html=frontend_html()
        digest=hashlib.sha256(html.encode()).hexdigest()[:16]
        directory=Path(tempfile.gettempdir())/("chokomana_kana_component_"+digest)
        directory.mkdir(parents=True,exist_ok=True)
        index=directory/"index.html"
        if not index.exists() or index.stat().st_size!=len(html.encode()):index.write_text(html,encoding="utf-8")
        return components.declare_component("chokomana_kana_"+digest,path=str(directory))
    component=get_component()
    try:
        hub=_safe_https_url(str(st.secrets.get("CHOKOMANA_HOME_URL",DEFAULT_HUB_URL))) or DEFAULT_HUB_URL
    except Exception:hub=DEFAULT_HUB_URL
    last_result=st.session_state.get("_kana_last_result",None)
    message=component(result=last_result,chokomana_home_url=hub,
       server_startup_ms=round((time.perf_counter()-run_t0)*1000,2),default=None,key="kana_client")
    if isinstance(message,dict) and message.get("delivery_id")!=st.session_state.get("_kana_last_delivery"):
        start=time.perf_counter()
        result=process_message(st,message)
        st.session_state["_kana_last_result"]=result
        st.session_state["_kana_last_delivery"]=message.get("delivery_id")
        # In the next rerun the component receives the ACK. This is the only
        # server-side rerun on normal quiz navigation, not per answer.
        st.rerun()

if __name__=="__main__":
    _run_streamlit()
