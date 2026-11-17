# -*- coding: utf-8 -*-
"""音声入力からA4硬筆PDFを作るアプリ。チャレンジ・採点はありません。"""
from __future__ import annotations
import base64, hashlib, io, json, os, re, sys, tempfile, time, unicodedata, uuid
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import datetime as dt
APP_VERSION = "kana-kouhitsu-1.9"
DEFAULT_HUB_URL = "https://chokomana-hkgx4uyxxezjtr2sj35jwj.streamlit.app/"
MAX_WORKSHEET_CHARS = 60

SOURCE_APP = "moji"
CONTENT_VERSION = "moji-kouhitsu-1"
DEFAULT_APP_URL = "https://chokomanakokugomoji-r8ue55i57c64ebtwonumqz.streamlit.app/"
MENUS = []
for _script,_label in (("hiragana","ひらがな"),("katakana","カタカナ")):
    MENUS.append(dict(menu_id=f"moji.{_script}.handwriting",category_id=_script+"_handwriting",category_label=_label+"・硬筆",label=_label+"の プリントを1枚かこう",
      difficulty_id="handwriting",difficulty_order=1,progression_group_id="moji."+_script+".handwriting",skill_ids=["moji."+_script+".handwriting"],
      mode_options=["learn"],assessment_method="self_report",estimated_duration_ms=300000,duration_basis="provisional",
      duration_rationale="1プリント5分の仮の目安。紙で書いた時間は未計測。",question_count_default=1,assessment_unit="print",
      next_menu_ids=[],is_active=True,supports_direct_launch=True,launch_url=DEFAULT_APP_URL,
      selection_instructions=[_label+"を選ぶ","ことばのプリントを作る","紙に書いて、今日のプリントでプリント全体を確認する"]))
CATALOG_ROWS = []
for _m in MENUS:
    CATALOG_ROWS.append(dict(schema_version=1,source_app=SOURCE_APP,entry_type="menu",entry_id=_m["menu_id"],content_version=CONTENT_VERSION,payload=_m))
    _skill=_m["skill_ids"][0]
    CATALOG_ROWS.append(dict(schema_version=1,source_app=SOURCE_APP,entry_type="skill",entry_id=_skill,content_version=CONTENT_VERSION,payload=dict(skill_id=_skill,label=_m["category_label"],assessment_method="self_report")))
    _script=_m["menu_id"].split('.')[1]
    _chars=[chr(i) for i in (range(0x3041,0x3097) if _script=="hiragana" else range(0x30a1,0x30f7))]+["ー"]
    for _char in _chars:
        _qid=f"moji.{_script}.u{ord(_char):x}"
        CATALOG_ROWS.append(dict(schema_version=1,source_app=SOURCE_APP,entry_type="question",entry_id=_qid,content_version=CONTENT_VERSION,payload=dict(question_id=_qid,character=_char,category_id=_m["category_id"],difficulty_id="handwriting",skill_ids=[_skill],question_family_id="moji."+_script+".handwriting",assessment_method="self_report",correct_answer_spec=None)))

def _moji_config(secrets):
    try:
        url=str(secrets.get("SUPABASE_URL","")).rstrip("/")
        key=str(secrets.get("SUPABASE_SERVICE_ROLE_KEY","") or secrets.get("SUPABASE_SECRET_KEY","") or secrets.get("SUPABASE_KEY",""))
    except (FileNotFoundError,KeyError):return "",""
    return (url,key) if _safe_https_url(url) and key else ("","")

def _moji_request(secrets,path,payload,*,upsert=False):
    url,key=_moji_config(secrets)
    if not url:raise ValueError("not_configured")
    headers={"apikey":key,"Authorization":"Bearer "+key,"Content-Type":"application/json"}
    if upsert:headers['Prefer']='resolution=merge-duplicates,return=minimal'
    request=Request(url+path,data=json.dumps(payload,ensure_ascii=False).encode(),headers=headers,method="POST")
    with urlopen(request,timeout=6) as response:
        body=response.read()
    return json.loads(body) if body else None

def _history_hash(token):
    if not isinstance(token,str) or not re.fullmatch(r"[0-9a-f]{64}",token):raise ValueError("invalid_device")
    return hashlib.sha256(("kana:print-history:v1:"+token).encode()).hexdigest()

def moji_identity(st,message):
    proof=_history_hash(message.get('history_token'))
    if not _moji_config(st.secrets)[0]:return dict(ok=True,linked=False,code='not_configured')
    raw=st.session_state.get('_moji_ticket')
    ticket_hash=hashlib.sha256(raw.encode()).hexdigest() if raw else None
    data=_moji_request(st.secrets,'/rest/v1/rpc/chokomana_moji_claim_launch_v1',{'p_device_hash':proof,'p_ticket_hash':ticket_hash})
    if not isinstance(data,dict):raise ValueError('invalid_identity')
    if data.get('linked'):
        person=str(uuid.UUID(data['profile_id']))
        plan=data.get('plan_launch')
        if plan and (plan.get('menu_id') not in {m['menu_id'] for m in MENUS} or plan.get('content_version')!=CONTENT_VERSION):
            plan=None;data['plan_notice']='教材が更新されています。ちょこまなで学習記録を更新してください。'
        st.session_state['_moji_identity']={'profile_id':person,'device_hash':proof,'launch_hash':ticket_hash,'plan_launch':plan}
        st.session_state.pop('_moji_ticket',None)
        if 'cm_launch' in st.query_params:del st.query_params['cm_launch']
        rows=[{**r,'payload':dict(r['payload'])} for r in CATALOG_ROWS]
        app_url=_safe_https_url(str(st.secrets.get('MOJI_APP_URL',DEFAULT_APP_URL))) or DEFAULT_APP_URL
        for r in rows:
            if r['entry_type']=='menu':r['payload']['launch_url']=app_url
        try:
            _moji_request(st.secrets,'/rest/v1/chokomana_learning_catalog?on_conflict=source_app,entry_type,entry_id,content_version',rows,upsert=True)
        except Exception:
            data['catalog_pending']=True
        data['plan_launch']=plan
    return dict(data,ok=True,code='linked' if data.get('linked') else 'standalone')


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
    """Fill each A4 page with 8 x 12 writing cells, alternating trace/practice."""
    if orientation not in ("horizontal", "vertical"):raise ValueError("invalid_orientation")
    if not 1 <= len(word) <= MAX_WORKSHEET_CHARS:raise ValueError("invalid_length")
    axis = 8 if orientation == "horizontal" else 12
    bands = 6 if orientation == "horizontal" else 4
    chunks = [word[i:i+axis] for i in range(0,len(word),axis)]
    copies = (axis + 1) // (len(word) + 1) if len(chunks) == 1 else 1
    repeats = max(1,bands // len(chunks))
    lines = [(" ".join([chunk] * copies)).ljust(axis) for _ in range(repeats) for chunk in chunks]
    pages=[]
    for offset in range(0,len(lines),bands):
        page=[["" for _ in range(8)] for _ in range(12)]
        for band,line in enumerate(lines[offset:offset+bands]):
            for pos,ch in enumerate(line):
                row,col=(band*2,pos) if orientation=="horizontal" else (pos,band*2)
                page[row][col]=ch.strip()
        pages.append(page)
    return {"word":word,"orientation":orientation,"repeats":repeats*copies,
            "chunks":chunks,"max_per_line":axis,"pages":pages}

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
    cell=min(23*mm,(width-2*margin)/8,(height-2*margin)/12)
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

    for page_index,page in enumerate(plan["pages"]):
        if page_index:pdf.showPage()
        for row in range(12):
            for col in range(8):
                x=margin+col*cell if orientation=="horizontal" else width-margin-(col+1)*cell
                box(x,height-margin-(row+1)*cell,page[row][col] or None)
    pdf.save()
    return buf.getvalue()

def _utc_timestamp(value):
    if not isinstance(value,str) or len(value)>64:raise ValueError("invalid_time")
    parsed=dt.datetime.fromisoformat(value.replace("Z","+00:00"))
    if parsed.tzinfo is None:raise ValueError("invalid_time")
    return parsed.astimezone(dt.timezone.utc).isoformat().replace("+00:00","Z")


def sync_print_history(secrets,message,identity=None):
    token=message.get("history_token","")
    if not isinstance(token,str) or not re.fullmatch(r"[0-9a-f]{64}",token):raise ValueError("invalid_device")
    prints=message.get("prints",[]);awards=message.get("awards",[])
    if not isinstance(prints,list) or not isinstance(awards,list) or len(prints)>20 or len(awards)>200:raise ValueError("invalid_batch")
    clean_prints=[];clean_awards=[]
    for item in prints:
        word=normalize_worksheet_text(item["word"],item["script"])
        if word!=item["word"] or item["orientation"] not in ("horizontal","vertical"):raise ValueError("invalid_print")
        clean_prints.append({"print_id":str(uuid.UUID(item["print_id"])),"word":word,"script":item["script"],"orientation":item["orientation"],"printed_at":_utc_timestamp(item["printed_at"]),"plan_item_id":item.get("plan_item_id")})
    for item in awards:
        if item.get("unit")!="print":raise ValueError("invalid_award_unit")
        clean_awards.append({"print_id":str(uuid.UUID(item["print_id"])),"unit":"print","awarded_at":_utc_timestamp(item["awarded_at"])})
    if identity:
        proof=_history_hash(token)
        if proof!=identity['device_hash']:raise ValueError('invalid_device')
        data=_moji_request(secrets,'/rest/v1/rpc/chokomana_moji_sync_sheet_history_v1',dict(p_device_hash=proof,p_profile_id=identity['profile_id'],p_prints=clean_prints,p_awards=clean_awards,p_launch_hash=identity.get('launch_hash')))
        if not isinstance(data,dict) or not all(isinstance(data.get(k),list) for k in ('prints','awards','acked_print_ids','acked_award_ids')):raise ValueError('invalid_response')
        return {'ok':True,'code':'saved','history':data,'linked':True,'profile_id':identity['profile_id']}
    try:
        url=str(secrets.get("SUPABASE_URL","")).rstrip("/")
        key=str(secrets.get("SUPABASE_SERVICE_ROLE_KEY","") or secrets.get("SUPABASE_SECRET_KEY","") or secrets.get("SUPABASE_KEY",""))
    except FileNotFoundError:return {"ok":False,"code":"not_configured"}
    if not _safe_https_url(url) or not key:return {"ok":False,"code":"not_configured"}
    payload={"p_device_hash":hashlib.sha256(("kana:print-history:v1:"+token).encode()).hexdigest(),"p_prints":clean_prints,"p_awards":clean_awards}
    request=Request(url+"/rest/v1/rpc/kana_sync_sheet_history_v1",data=json.dumps(payload,ensure_ascii=False).encode(),headers={"apikey":key,"Authorization":"Bearer "+key,"Content-Type":"application/json"},method="POST")
    with urlopen(request,timeout=3) as response:data=json.load(response)
    if not isinstance(data,dict) or not all(isinstance(data.get(k),list) for k in ("prints","awards","acked_print_ids","acked_award_ids")):raise ValueError("invalid_response")
    return {"ok":True,"code":"saved","history":data}


def process_message(st, message):
    result={"delivery_id":message.get("delivery_id"),"ok":False,"code":"invalid_input","revision":message.get("revision")}
    try:
        uuid.UUID(str(message.get("delivery_id")))
        if message.get("kind")=="portal_hello":
            result.update(moji_identity(st,message))
        elif message.get("kind")=="history_sync":
            identity=st.session_state.get('_moji_identity')
            if message.get('profile_id') and (not identity or message['profile_id']!=identity['profile_id']):
                raise ValueError('learning owner mismatch')
            result.update(sync_print_history(st.secrets,message,identity))
        elif message.get("kind")=="normalize":
            result.update(ok=True,normalized=normalize_spoken_word(message.get("text",""),message.get("script",""),message.get("word_mode","auto")))
        elif message.get("kind")=="pdf":
            pdf=generate_worksheet_pdf(message.get("text",""),message.get("script",""),message.get("orientation",""))
            result.update(ok=True,pdf_base64=base64.b64encode(pdf).decode("ascii"),printed_at=dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00","Z"))
    except (HTTPError,URLError,TimeoutError):result["code"]="sync_unavailable"
    except Exception:pass
    return result


HTML_TEMPLATE = '<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><style>\n:root{color-scheme:light;--blue:#315f86;--line:#ded6c9}*{box-sizing:border-box}body{margin:0;background:#f7f5ef;color:#292b2a;font:16px system-ui,-apple-system,\'Yu Gothic\',sans-serif}main{max-width:940px;margin:auto;padding:16px 16px 36px}header{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap}header .small{flex:1}h1{font-size:27px;margin:12px 0 20px}h2{font-size:19px;margin:0 0 20px}button,a{font:inherit;touch-action:manipulation}button,.btn{min-height:46px;padding:10px 14px;border:1px solid var(--line);border-radius:12px;background:#fff;color:#263442;cursor:pointer;text-decoration:none;display:inline-flex;align-items:center;justify-content:center}button:focus-visible,a:focus-visible{outline:3px solid #76a7d2;outline-offset:2px}button.primary{background:var(--blue);color:white;border-color:var(--blue);font-weight:700}button[aria-pressed=true]{background:#e5eff6;border:2px solid #467fa8}button:disabled{opacity:.5;cursor:default}.card{background:#fffefa;border:1px solid var(--line);border-radius:19px;padding:22px}.seg{display:flex;gap:8px;margin:8px 0 18px}.seg button{flex:1}.label{font-size:14px;color:#53616a}input{font:26px system-ui;width:100%;padding:14px;border:1px solid #b8c0c7;border-radius:12px;background:#fff;color:#20282e;margin:8px 0 10px}p{font-size:13px;color:#647078;line-height:1.7}.actions{display:flex;align-items:center;flex-wrap:wrap;gap:10px;margin:12px 0}.status{font-size:14px;color:#53616a;min-height:22px}.candidates{display:flex;flex-wrap:wrap;gap:6px}.candidates button{font-size:16px;padding:6px 12px}.preview{background:white;aspect-ratio:210/297;width:100%;max-width:410px;margin:14px auto 22px;border:1px solid #ccc;padding:6.66%;overflow:hidden;--cell:clamp(18px,7vw,38px);--group-gap:calc(var(--cell)*.73)}.cells{display:flex}.cell{width:var(--cell);height:var(--cell);position:relative;border:1px solid #ccc;flex:none;display:grid;place-items:center;color:#b3b3b3;font-size:calc(var(--cell)*.64);font-family:serif}.cell:before,.cell:after{content:\'\';position:absolute;pointer-events:none}.cell:before{height:100%;border-left:1px dashed #e8e8e8;left:50%}.cell:after{width:100%;border-top:1px dashed #e8e8e8;top:50%}.cell span{z-index:1}.trace-group{display:flex;gap:calc(var(--cell)*.32)}.horizontal .trace-group{flex-direction:column;margin-bottom:var(--group-gap)}.vertical{display:flex;flex-direction:row-reverse;gap:var(--group-gap);justify-content:flex-start}.vertical .trace-group{flex-direction:row-reverse}.vertical .cells{flex-direction:column}.vertical .longmark{transform:rotate(90deg)}details{margin-top:18px}summary{cursor:pointer;font-size:14px;color:#53616a;padding:8px 0}[hidden]{display:none!important}.small{font-size:12px}.error{color:#a54232}@media(max-width:480px){main{padding:10px}.card{padding:16px}h1{font-size:24px}.preview{padding:6.66%;--cell:clamp(18px,7.5vw,38px)}header .btn{font-size:13px;padding:8px}}\n .preview.page{position:relative;aspect-ratio:210/297;padding:0;container-type:inline-size;overflow:hidden}.page .cell{font-size:6.83cqw}.preview-pages{width:100%}\n.print-record{border:1px solid var(--line);border-radius:14px;padding:16px;margin:16px 0}.print-record h3{margin:0 0 5px;overflow-wrap:anywhere}.char-award{display:grid;grid-template-columns:70px 1fr;align-items:center;gap:12px;padding:9px 0;border-top:1px solid #eee}.char-symbol{font-size:30px;color:#293944}.char-number{font-size:12px;color:#647078}.char-award button{font-size:15px;min-height:46px}.sheet-award.earned{opacity:1;background:#fff1bf;color:#725000;border-color:#e0ba4e}.medal-summary{padding:12px;background:#fff7dc;border-radius:12px;font-weight:700}.tabs{margin-bottom:16px}.tabs button[aria-pressed=true]{background:var(--blue);border-color:var(--blue);color:#fff}\n.sheet-award{width:100%;margin-top:10px;font-weight:700}\n</style></head><body><main id="app"><header><span class="small">ちょこまな 学習アプリ</span><button id="settingsButton" type="button">設定</button><a id="hub" class="btn" target="_blank" rel="noopener noreferrer" href="https://chokomana-hkgx4uyxxezjtr2sj35jwj.streamlit.app/">← ちょこまなへ</a></header><p id="portalStatus" class="status" role="status">ちょこまな連携を かくにんしています…</p><h1>ひらがな・カタカナ</h1><nav class="seg tabs" aria-label="ページ"><button id="makeTab" aria-pressed="true">プリントを つくる</button><button id="historyTab" aria-pressed="false">今日のプリント</button></nav><section id="makePane" class="card"><h2>こうひつ プリント</h2>\n<div class="label">もじ</div><div class="seg" id="scripts"><button data-script="hiragana" aria-pressed="true">ひらがな</button><button data-script="katakana" aria-pressed="false">カタカナ</button></div>\n<label class="label" for="word">ことばを いってね</label><input id="word" placeholder="はなすか、もじを いれてね" maxlength="120" autocomplete="off"><div class="actions"><button id="mic" class="primary" aria-pressed="false">マイクで はなす</button><button id="resetWord" type="button">もじを リセット</button><span id="voice" class="status" role="status" aria-live="polite"></span></div><div id="candidates" class="candidates" aria-label="ほかの ききとり こうほ"></div>\n<details><summary>ことばの あつかい</summary><div class="seg" id="modes"><button data-mode="auto" aria-pressed="true">じどう</button><button data-mode="usual" aria-pressed="false">ふつう</button><button data-mode="playful" aria-pressed="false">おもしろ</button></div><p>おもしろい ことばは、きこえた おとを だいじにします。ちがう ときは、もじを そのまま なおせます。</p></details>\n<div class="label">かきかた</div><div class="seg" id="directions"><button data-dir="horizontal" aria-pressed="true">よこがき</button><button data-dir="vertical" aria-pressed="false">たてがき</button></div>\n<div id="info" class="status" role="status" aria-live="polite"></div><div id="previewSection" hidden><div class="label">いんさつ まえの プレビュー</div><div id="preview" class="preview horizontal"></div><p>ことばが ちがう ときは、うえの もじを なおしてね。</p><button id="pdf" class="primary">PDFを ダウンロード</button><div id="pdfStatus" class="status" role="status" aria-live="polite"></div></div>\n</section><section id="historyPane" class="card" hidden><h2>今日のプリント</h2><p id="historyDate"></p><p>PDFを だした ことばが のこります。プリントが きれいに かけたら「きれいに かけた！」を おしてね。プリント1まいに きんメダル1こが もらえます。</p><div id="medalSummary" class="medal-summary" role="status" aria-live="polite"></div><div id="historyList"></div><p id="historySync" class="status" role="status"></p></section><section id="settingsPane" class="card" hidden><div class="actions"><h2 style="margin:0;flex:1">設定・動作ログ</h2><button id="settingsBack" type="button">もどる</button></div><p>速度・操作・かな変換・通信エラーを記録します。入力した言葉・音声・認証情報は含めません。</p><p id="diagnosticStatus" role="status"></p><pre id="timingSummary" style="white-space:pre-wrap;font:inherit;font-size:14px"></pre><div class="actions"><button id="logs" class="primary">ログを出力（JSON）</button><button id="logsCsv">ログを出力（CSV）</button></div><p>問題が起きた直後に出力してください。時間の単位はミリ秒です。画面表示時間は描画フレームによる推定で、外部アプリのタップから起動するまでの時間は計測できません。</p></section></main>\n<script>\n\'use strict\';\nconst $=s=>document.querySelector(s),$$=s=>Array.from(document.querySelectorAll(s));\nlet script=\'hiragana\',orientation=\'horizontal\',mode=\'auto\',revision=0,word=\'\',timer=null,active=null,queue=[],lastReply=\'\',pdfSnapshot=null,resetEpoch=0;\nlet recognition=null,micWanted=false,run=0,retryTimer=null,watchdog=null,retries=0,gotResult=false,logs=[];\nconst uuid=()=>crypto.randomUUID();\nconst diagnosticSession=uuid();let diagnosticSaveTimer=null,diagnosticStorageError=false,inputStartedAt=0,micRequestedAt=0,requestTimer=null,sendTimer=null,transportSequence=0,settingsReturnToHistory=false;\nconst DIAGNOSTIC_KEY=\'kana_kouhitsu_diagnostics_v11\';\nconst LOG_FIELDS=new Set([\'kind\',\'code\',\'ok\',\'attempt\',\'duration_ms\',\'queue_ms\',\'server_ms\',\'queue_length\',\'characters\',\'direction\',\'candidates\',\'character_index\',\'control\',\'measurement\',\'visibility\']);\nfunction cleanDiagnostic(data){const out={};for(const [key,value]of Object.entries(data||{}))if(LOG_FIELDS.has(key)&&(value===null||typeof value===\'boolean\'||(typeof value===\'number\'&&Number.isFinite(value))||(typeof value===\'string\'&&/^[A-Za-z0-9_:. -]{0,100}$/.test(value))))out[key]=value;return out}\nfunction flushDiagnostics(){clearTimeout(diagnosticSaveTimer);diagnosticSaveTimer=null;try{localStorage.setItem(DIAGNOSTIC_KEY,JSON.stringify(logs));diagnosticStorageError=false}catch{diagnosticStorageError=true}}\nfunction diag(type,data={}){logs.push({at:new Date().toISOString(),session:diagnosticSession,type,...cleanDiagnostic(data)});logs=logs.slice(-2000);if(!diagnosticSaveTimer)diagnosticSaveTimer=setTimeout(flushDiagnostics,500)}\ntry{const saved=JSON.parse(localStorage.getItem(DIAGNOSTIC_KEY)||\'[]\');logs=Array.isArray(saved)?saved.slice(-2000).map(r=>({at:r.at,type:r.type,session:r.session||null,...cleanDiagnostic(r)})):[]}catch{logs=[]}\nfunction diagnosticSummary(){const lines=[\'記録：\'+logs.length+\'件\'+(diagnosticStorageError?\'（端末保存不可・出力は可能）\':\'\')];for(const [kind,label]of [[\'preview_ready\',\'入力→プレビュー表示\'],[\'normalize\',\'かな変換の応答\'],[\'pdf\',\'PDFの応答\'],[\'history_sync\',\'履歴保存の応答\']]){const arr=logs.filter(r=>r.session===diagnosticSession&&(kind===\'preview_ready\'?r.type===kind:r.type===\'request_result\'&&r.kind===kind)&&Number.isFinite(r.duration_ms)).map(r=>r.duration_ms).sort((a,b)=>a-b);lines.push(label+\'：\'+(arr.length?\'中央値 \'+arr[Math.floor(arr.length/2)].toFixed(0)+\' ms／P95 \'+arr[Math.ceil(arr.length*.95)-1].toFixed(0)+\' ms\':\'未計測\'))}lines.push(\'通信待ち：\'+(active?active.kind+\'／\'+Math.round(performance.now()-active._started_at)+\' ms\':\'なし\')+\'、待ち列 \'+queue.length+\'件\');return lines.join(\'\\n\')}\nfunction showSettings(show){if(show){settingsReturnToHistory=!$(\'#historyPane\').hidden;$(\'#makePane\').hidden=true;$(\'#historyPane\').hidden=true;$(\'#settingsPane\').hidden=false;$(\'#timingSummary\').textContent=diagnosticSummary();$(\'#diagnosticStatus\').textContent=\'直近2,000件を記録。問題の直後に出力してください。\'}else{$(\'#settingsPane\').hidden=true;historyTab(settingsReturnToHistory)}height()}\nfunction exportDiagnostics(format){flushDiagnostics();const snapshot={schema_version:2,app_version:\'kana-kouhitsu-1.9\',exported_at:new Date().toISOString(),environment:{user_agent:navigator.userAgent,language:navigator.language,viewport:{width:innerWidth,height:innerHeight},visibility:document.visibilityState},state:{active_kind:active?.kind||null,active_elapsed_ms:active?Math.round(performance.now()-active._started_at):null,queued_kinds:queue.map(r=>r.kind)},measurement:{time_unit:\'milliseconds\',preview:\'two_animation_frames_estimate\',external_app_tap:\'not_observable\'},entries:logs};if(format===\'json\'){download(\'kana_diagnostics.json\',JSON.stringify(snapshot,null,2),\'application/json\')}else{const keys=[\'at\',\'session\',\'type\',\'kind\',\'control\',\'duration_ms\',\'queue_ms\',\'server_ms\',\'attempt\',\'ok\',\'code\',\'queue_length\',\'characters\',\'direction\'],quote=v=>\'"\'+String(v??\'\').replace(/"/g,\'""\')+\'"\';download(\'kana_diagnostics.csv\',\'\\uFEFF\'+[keys.join(\',\'),...logs.map(r=>keys.map(k=>quote(r[k])).join(\',\'))].join(\'\\r\\n\'),\'text/csv;charset=utf-8\')}}\nfunction initDiagnostics(){document.addEventListener(\'click\',event=>{const button=event.target.closest?.(\'button\');if(!button)return;const control=button.id||([\'script\',\'dir\',\'mode\'].find(k=>button.dataset[k])||\'dynamic_button\');const start=performance.now();diag(\'ui_action\',{control});requestAnimationFrame(()=>requestAnimationFrame(()=>diag(\'ui_frame_ready\',{control,duration_ms:+(performance.now()-start).toFixed(2),measurement:\'two_animation_frames_estimate\'})))},true);if(window.PerformanceObserver)try{new PerformanceObserver(list=>list.getEntries().forEach(e=>diag(\'long_task\',{duration_ms:+e.duration.toFixed(2)}))).observe({type:\'longtask\',buffered:true})}catch{}let tick=performance.now();setInterval(()=>{const now=performance.now(),lag=now-tick-1000;tick=now;if(!document.hidden&&lag>100)diag(\'event_loop_delay\',{duration_ms:+lag.toFixed(2)})},1000);window.addEventListener(\'pagehide\',flushDiagnostics);document.addEventListener(\'visibilitychange\',()=>{diag(\'visibility_changed\',{visibility:document.visibilityState});if(document.hidden)flushDiagnostics()});requestAnimationFrame(()=>requestAnimationFrame(()=>diag(\'startup_ready\',{duration_ms:+performance.now().toFixed(2),measurement:\'iframe_navigation_estimate\'})))}\nfunction height(){requestAnimationFrame(()=>parent.postMessage({isStreamlitMessage:true,type:\'streamlit:setFrameHeight\',height:document.documentElement.scrollHeight},\'*\'))}\nfunction bridge(kind,fields){const m={delivery_id:uuid(),kind,...fields,_queued_at:performance.now(),_attempts:0};if(kind===\'normalize\')queue=queue.filter(x=>x.kind!==\'normalize\');if(kind===\'normalize\'||kind===\'pdf\')queue.unshift(m);else queue.push(m);diag(\'request_queued\',{kind,queue_length:queue.length});pump()}\nfunction pump(){if(active||!queue.length)return;active=queue.shift();active._started_at=performance.now();diag(\'request_started\',{kind:active.kind,queue_ms:+(active._started_at-active._queued_at).toFixed(2),queue_length:queue.length});sendActive()}\nfunction sendActive(){clearTimeout(sendTimer);const req=active;if(!req)return;sendTimer=setTimeout(()=>{if(active!==req)return;req._attempts++;const payload={};for(const [key,value]of Object.entries(req))if(!key.startsWith(\'_\'))payload[key]=value;payload.transport_sequence=++transportSequence;diag(\'request_sent\',{kind:req.kind,attempt:req._attempts});parent.postMessage({isStreamlitMessage:true,type:\'streamlit:setComponentValue\',value:payload},\'*\');clearTimeout(requestTimer);requestTimer=setTimeout(()=>{if(active!==req)return;diag(\'request_timeout\',{kind:req.kind,attempt:req._attempts,duration_ms:+(performance.now()-req._started_at).toFixed(2)});if(req._attempts<3)sendActive();else onReply({delivery_id:req.delivery_id,ok:false,code:\'bridge_timeout\'})},req.kind===\'normalize\'?5000:15000)},0)}\nfunction cancelTextRequest(){queue=queue.filter(req=>req.kind!==\'normalize\'&&req.kind!==\'pdf\');if(active&&[\'normalize\',\'pdf\'].includes(active.kind)){diag(\'request_cancelled\',{kind:active.kind});clearTimeout(requestTimer);clearTimeout(sendTimer);active=null;setTimeout(pump,0)}}\nfunction select(group,attr,value){$$(group+\' button\').forEach(b=>b.setAttribute(\'aria-pressed\',String(b.dataset[attr]===value)))}\nfunction normalize(s){s=String(s).normalize(\'NFKC\').replace(/[。．、，!！?？"\'「」『』（）()\\[\\]【】\\s]/g,\'\');s=Array.from(s).map(x=>script===\'hiragana\'&&x>=\'ァ\'&&x<=\'ヶ\'?String.fromCharCode(x.charCodeAt(0)-96):script===\'katakana\'&&x>=\'ぁ\'&&x<=\'ゖ\'?String.fromCharCode(x.charCodeAt(0)+96):x).join(\'\');if(!s||s.length>60||!(script===\'hiragana\'?/^[ぁ-ゖー]+$/:/^[ァ-ヶー]+$/).test(s))throw Error(\'ひらがな・カタカナで 1〜60もじ いれてね\');return s}\nfunction clearPreview(){word=\'\';$(\'#previewSection\').hidden=true;height()}\nfunction update(){clearTimeout(timer);if(!inputStartedAt)inputStartedAt=performance.now();revision++;const text=$(\'#word\').value;$(\'#pdfStatus\').textContent=\'\';if(!text.trim()){clearPreview();$(\'#info\').textContent=\'\';return}\n if(/[\\u3400-\\u9fff\\uf900-\\ufaff]/.test(text)){clearPreview();$(\'#info\').textContent=\'かなに なおしています…\';bridge(\'normalize\',{text,script,word_mode:mode,revision});return}\n try{word=normalize(text);$(\'#word\').value=word;$(\'#info\').textContent=\'\';draw()}catch(e){clearPreview();$(\'#info\').textContent=e.message}}\nfunction paperPlan(text,dir){\n const chars=Array.from(text),axis=dir===\'horizontal\'?8:12,bands=dir===\'horizontal\'?6:4,chunks=[];\n for(let i=0;i<chars.length;i+=axis)chunks.push(chars.slice(i,i+axis));\n const copies=chunks.length===1?Math.floor((axis+1)/(chars.length+1)):1,repeats=Math.max(1,Math.floor(bands/chunks.length)),lines=[];\n for(let n=0;n<repeats;n++)for(const chunk of chunks){let line=[];for(let k=0;k<copies;k++){if(k)line.push(\'\');line.push(...chunk)}while(line.length<axis)line.push(\'\');lines.push(line)}\n const pages=[];for(let offset=0;offset<lines.length;offset+=bands){const page=Array.from({length:12},()=>Array(8).fill(\'\'));lines.slice(offset,offset+bands).forEach((line,band)=>line.forEach((ch,pos)=>{const row=dir===\'horizontal\'?band*2:pos,col=dir===\'horizontal\'?pos:band*2;page[row][col]=ch}));pages.push(page)}return pages;\n}\nfunction draw(){const out=$(\'#preview\');out.textContent=\'\';out.className=\'preview-pages\';out.removeAttribute(\'style\');\n const mm=72/25.4,W=595.275590551,H=841.88976378,margin=14*mm,cell=Math.min(23*mm,(W-2*margin)/8,(H-2*margin)/12);\n for(const cells of paperPlan(word,orientation)){const page=document.createElement(\'div\');page.className=\'preview page\';out.append(page);\n for(let row=0;row<12;row++)for(let col=0;col<8;col++){const ch=cells[row][col],x=orientation===\'horizontal\'?margin+col*cell:W-margin-(col+1)*cell;\n const el=document.createElement(\'div\');el.className=\'cell\';Object.assign(el.style,{position:\'absolute\',left:(x/W*100)+\'%\',top:((margin+row*cell)/H*100)+\'%\',width:(cell/W*100)+\'%\',height:(cell/H*100)+\'%\'});\n const span=document.createElement(\'span\');span.textContent=ch;if(orientation===\'vertical\'&&ch===\'ー\')span.className=\'longmark\';el.append(span);page.append(el)}}\n $(\'#previewSection\').hidden=false;$(\'#pdf\').disabled=false;height();diag(\'preview_updated\',{direction:orientation,characters:Array.from(word).length});const start=inputStartedAt||performance.now(),generation=revision;requestAnimationFrame(()=>requestAnimationFrame(()=>{if(generation===revision&&!$(\'#previewSection\').hidden){diag(\'preview_ready\',{duration_ms:+(performance.now()-start).toFixed(2),characters:Array.from(word).length,direction:orientation});inputStartedAt=0}}))}\nfunction download(name,bytes,type){const url=URL.createObjectURL(new Blob([bytes],{type})),a=document.createElement(\'a\');a.href=url;a.download=name;document.body.append(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),60000)}\nfunction requestPDF(){clearTimeout(timer);if(!word||$(\'#previewSection\').hidden)return;stopMic();pdfSnapshot={text:word,script,orientation,reset_epoch:resetEpoch};$(\'#pdf\').disabled=true;$(\'#pdfStatus\').textContent=\'PDFを つくっています…\';bridge(\'pdf\',pdfSnapshot)}\nfunction onReply(result){if(!result||!active||result.delivery_id!==active.delivery_id||result.delivery_id===lastReply)return;lastReply=result.delivery_id;const req=active;active=null;clearTimeout(requestTimer);clearTimeout(sendTimer);diag(\'request_result\',{kind:req.kind,ok:result.ok===true,code:result.code||\'ok\',attempt:req._attempts,duration_ms:+(performance.now()-req._started_at).toFixed(2),server_ms:result.server_ms??null});try{\n if(req.kind===\'normalize\'&&req.revision===revision&&req.text===$(\'#word\').value&&req.script===script){if(result.ok&&typeof result.normalized?.text===\'string\'){$(\'#word\').value=result.normalized.text;word=result.normalized.text;$(\'#voice\').textContent=\'ききとりました。\';$(\'#info\').textContent=\'よみを かくにんしてね。ちがう ときは うえで なおせます。\';draw()}else{$(\'#info\').textContent=\'かなへの へんかんに しっぱいしました。もういちど はなすか、もじを なおしてね。\';clearPreview()}}\n if(req.kind===\'pdf\'&&req.reset_epoch===resetEpoch){$(\'#pdf\').disabled=false;if(result.ok){const bytes=Uint8Array.from(atob(result.pdf_base64),c=>c.charCodeAt(0));download(\'こうひつ_\'+req.text.slice(0,20)+\'.pdf\',bytes,\'application/pdf\');rememberPrint(req,result.printed_at);$(\'#pdfStatus\').textContent=\'PDFを つくりました。A4で いんさつしてね。\';diag(\'pdf_created\',{direction:req.orientation})}else{$(\'#pdfStatus\').textContent=\'PDFを つくれませんでした。もういちど おしてね。\';diag(\'pdf_error\')}}if(req.kind===\'portal_hello\')onPortalReply(result);if(req.kind===\'history_sync\')onHistoryReply(result);}catch{diag(\'reply_handler_error\',{kind:req.kind});if(req.kind===\'normalize\')$(\'#info\').textContent=\'かなへの へんかんに しっぱいしました。もういちど はなしてね。\';}finally{setTimeout(pump,0);height()}}\nfunction micUI(text){$(\'#mic\').textContent=micWanted?\'マイクを とめる\':\'マイクで はなす\';$(\'#mic\').setAttribute(\'aria-pressed\',String(micWanted));if(text!==undefined)$(\'#voice\').textContent=text;height()}\nfunction detach(){clearTimeout(watchdog);watchdog=null;const r=recognition;recognition=null;if(r){r.onstart=r.onresult=r.onerror=r.onend=r.onspeechstart=r.onspeechend=null;try{r.abort()}catch{}}}\nfunction stopMic(){micWanted=false;run++;clearTimeout(retryTimer);detach();micUI(\'\')}\nfunction retry(token){if(!micWanted||token!==run||document.hidden)return;if(++retries>2){stopMic();micUI(\'ききとれませんでした。もういちど マイクを おしてね。\');return}retryTimer=setTimeout(()=>listen(token),300)}\nfunction listen(token){if(!micWanted||recognition||token!==run)return;const SR=window.SpeechRecognition||window.webkitSpeechRecognition;if(!SR){stopMic();micUI(\'このブラウザは おんせいに たいおうしていません。もじを いれてね。\');return}\n const r=new SR();recognition=r;gotResult=false;r.lang=\'ja-JP\';r.continuous=false;r.interimResults=true;r.maxAlternatives=5;const current=()=>recognition===r&&token===run;\n r.onstart=()=>{if(!current())return;clearTimeout(watchdog);watchdog=setTimeout(()=>{if(current()){detach();retry(token)}},20000);micUI(\'きいています…\');diag(\'microphone_started\',{duration_ms:micRequestedAt?+(performance.now()-micRequestedAt).toFixed(2):null})};\n r.onspeechstart=()=>{if(current())micUI(\'きいています…\')};\n r.onresult=e=>{if(!current())return;const finals=[],interims=[];let alternatives=[];for(let i=0;i<e.results.length;i++){const item=e.results[i];if(item.isFinal){finals.push(item[0]?.transcript||\'\');if(i===e.results.length-1)alternatives=Array.from(item).map(x=>({text:x.transcript,confidence:x.confidence||0}))}else interims.push(item[0]?.transcript||\'\')}\n  if(interims.length){micUI(\'ききとり：\'+finals.join(\'\')+interims.join(\'\'));return}if(!finals.length)return;\n  const prefix=finals.slice(0,-1).join(\'\');alternatives=alternatives.map(x=>({...x,text:prefix+x.text}));if(!alternatives.length)alternatives=[{text:finals.join(\'\'),confidence:0}];\n  if(mode===\'playful\'){const kana=alternatives.find(x=>/^[ぁ-ゖァ-ヶー\\s。、]+$/.test(x.text));if(kana)alternatives=[kana,...alternatives.filter(x=>x!==kana)]}\n  const unique=[...new Set(alternatives.map(x=>x.text.trim()).filter(Boolean))];gotResult=true;$(\'#word\').value=unique[0];$(\'#candidates\').textContent=\'\';for(const text of unique.slice(1,5)){const b=document.createElement(\'button\');b.textContent=text;b.onclick=()=>{$(\'#word\').value=text;$(\'#candidates\').textContent=\'\';update()};$(\'#candidates\').append(b)}\n  inputStartedAt=performance.now();update();micUI(/[\\u3400-\\u9fff\\uf900-\\ufaff]/.test(unique[0])?\'ききとりました。かなに なおしています…\':\'ききとりました。\');diag(\'microphone_result\',{candidates:unique.length,characters:unique[0].length,duration_ms:micRequestedAt?+(performance.now()-micRequestedAt).toFixed(2):null});\n  // Finish the browser callback before aborting (Android/Safari lifecycle).\n  setTimeout(()=>{if(current()){stopMic();micUI(\'ききとりました。\')}} ,0);\n };\n r.onerror=e=>{if(!current())return;const code=e.error||\'unknown\';diag(\'microphone_error\',{code});const fatal=[\'not-allowed\',\'service-not-allowed\',\'audio-capture\',\'language-not-supported\'].includes(code);detach();if(fatal){stopMic();micUI(code===\'network\'?\'おんせいの つうしんに しっぱいしました。もういちど マイクを おしてね。\':code===\'not-allowed\'||code===\'service-not-allowed\'?\'ブラウザで マイクを きょかしてね。\':\'マイクを かくにんしてね。\')}else{micUI(\'もういちど はなしてね。\');retry(token)}};\n r.onend=()=>{if(!current())return;const completed=gotResult;detach();if(completed){stopMic();micUI(\'ききとりました。\')}else retry(token)};\n try{r.start();watchdog=setTimeout(()=>{if(current()){detach();retry(token)}},6000)}catch(e){diag(\'microphone_error\',{code:e.name});detach();if([\'NotAllowedError\',\'SecurityError\'].includes(e.name)){stopMic();micUI(\'ブラウザで マイクを きょかしてね。\')}else retry(token)}\n}\nlet PRINT_DB_KEY=\'kana_print_history_v2\';const LEGACY_PRINT_DB_KEY=PRINT_DB_KEY,PRINT_TOKEN_KEY=\'kana_print_history_device_v1\';\nlet portalReady=false,portalLinked=false,portalProfile=null,portalPlan=null;\nconst LAST_PROFILE_KEY=\'moji_last_profile_v1\';\nlet printDB={prints:[],awards:[],ackedPrints:[],ackedAwards:[]},historyToken=\'\',historyTimer=null,midnightTimer=null;\nconst awardId=a=>a.print_id;\nfunction validPrint(p){return p&&typeof p.print_id===\'string\'&&/^[0-9a-f-]{36}$/i.test(p.print_id)&&typeof p.word===\'string\'&&/^[ぁ-ゖァ-ヶー]{1,60}$/.test(p.word)&&[\'hiragana\',\'katakana\'].includes(p.script)&&[\'horizontal\',\'vertical\'].includes(p.orientation)&&Number.isFinite(Date.parse(p.printed_at))}\nfunction validAward(a){return a&&typeof a.print_id===\'string\'&&a.unit===\'print\'&&Number.isFinite(Date.parse(a.awarded_at))}\nfunction mergeHistory(data){if(!data)return;const prints=new Map(printDB.prints.map(p=>[p.print_id,p]));for(const p of data.prints||[])if(validPrint(p))prints.set(p.print_id,p);printDB.prints=[...prints.values()];const awards=new Map(printDB.awards.map(a=>[awardId(a),a]));for(const a of data.awards||[]){const p=prints.get(a.print_id);if(validAward(a)&&p)awards.set(awardId(a),a)}printDB.awards=[...awards.values()];printDB.ackedPrints=[...new Set([...printDB.ackedPrints,...(data.ackedPrints||[])])];printDB.ackedAwards=[...new Set([...printDB.ackedAwards,...(data.ackedAwards||[])])];}\nfunction loadHistory(){try{const current=JSON.parse(localStorage.getItem(PRINT_DB_KEY)||\'null\');if(current)return current;const legacy=JSON.parse(localStorage.getItem(PRINT_DB_KEY.replace(\'kana_print_history_v2\',\'kana_print_history_v1\'))||\'null\');return legacy?{prints:legacy.prints||[],awards:[],ackedPrints:[],ackedAwards:[]}:null}catch{return null}}\nfunction persistHistory(){mergeHistory(loadHistory());try{localStorage.setItem(PRINT_DB_KEY,JSON.stringify(printDB));return true}catch{ $(\'#historySync\').textContent=\'この端末に ほぞんできません。データベースへの ほぞんを ためしています。\';return false}}\nfunction dateKey(value){const parts=new Intl.DateTimeFormat(\'en-CA\',{timeZone:\'Asia/Tokyo\',year:\'numeric\',month:\'2-digit\',day:\'2-digit\'}).formatToParts(new Date(value)),v={};parts.forEach(p=>v[p.type]=p.value);return v.year+\'-\'+v.month+\'-\'+v.day}\nfunction historyTab(show){$(\'#settingsPane\').hidden=true;$(\'#makePane\').hidden=show;$(\'#historyPane\').hidden=!show;$(\'#makeTab\').setAttribute(\'aria-pressed\',String(!show));$(\'#historyTab\').setAttribute(\'aria-pressed\',String(show));if(show)renderHistory();height()}\nfunction rememberPrint(req,printedAt){if(!printDB.prints.some(p=>p.print_id===req.delivery_id)){const p={print_id:req.delivery_id,word:req.text,script:req.script,orientation:req.orientation,printed_at:printedAt||new Date().toISOString(),plan_item_id:portalPlan?.menu_id===\'moji.\'+req.script+\'.handwriting\'?portalPlan.plan_item_id:null};if(validPrint(p))printDB.prints.push(p)}persistHistory();renderHistory();scheduleHistorySync();}\nfunction earnMedal(printId){if(!portalReady)return;const p=printDB.prints.find(p=>p.print_id===printId);if(!p||printDB.awards.some(a=>awardId(a)===printId))return;printDB.awards.push({print_id:printId,unit:\'print\',awarded_at:new Date().toISOString()});persistHistory();renderHistory();scheduleHistorySync();diag(\'print_gold_awarded\');}\nfunction renderHistory(){const today=dateKey(new Date()),list=$(\'#historyList\');$(\'#historyDate\').textContent=today+\'（日本時間）\';const prints=printDB.prints.filter(p=>dateKey(p.printed_at)===today).sort((a,b)=>Date.parse(b.printed_at)-Date.parse(a.printed_at)),ids=new Set(prints.map(p=>p.print_id)),awarded=new Set(printDB.awards.map(awardId));$(\'#medalSummary\').textContent=\'🥇 今日の きんメダル：\'+printDB.awards.filter(a=>ids.has(a.print_id)).length+\'こ\';list.textContent=\'\';if(!prints.length){const empty=document.createElement(\'p\');empty.textContent=\'今日のプリントは まだ ありません。PDFを だすと ここに のこります。\';list.append(empty)}\n for(const p of prints){const card=document.createElement(\'article\');card.className=\'print-record\';card.dataset.printId=p.print_id;const title=document.createElement(\'h3\');title.textContent=p.word;const time=document.createElement(\'p\');time.textContent=new Intl.DateTimeFormat(\'ja-JP\',{timeZone:\'Asia/Tokyo\',hour:\'2-digit\',minute:\'2-digit\'}).format(new Date(p.printed_at))+\' ／ \'+(p.orientation===\'vertical\'?\'たてがき\':\'よこがき\');card.append(title,time);const button=document.createElement(\'button\');button.type=\'button\';button.dataset.printId=p.print_id;const gold=awarded.has(p.print_id);button.textContent=gold?\'🥇 きんメダル かくとく！\':\'きれいに かけた！\';button.disabled=gold||!portalReady;button.className=\'sheet-award \'+(gold?\'earned\':\'primary\');button.setAttribute(\'aria-label\',p.word+\'のプリント、\'+(gold?\'きんメダル かくとく\':\'きれいに かけた\'));button.onclick=()=>earnMedal(p.print_id);card.append(button);list.append(card)}height();}\nfunction scheduleHistorySync(){clearTimeout(historyTimer);historyTimer=setTimeout(()=>syncHistory(false),100)}\nfunction syncHistory(force=false){if(!portalReady||(!portalLinked&&portalProfile)||!historyToken||queue.some(r=>r.kind===\'history_sync\')||active?.kind===\'history_sync\')return;const ackP=new Set(printDB.ackedPrints),ackA=new Set(printDB.ackedAwards),prints=printDB.prints.filter(p=>!ackP.has(p.print_id)).slice(0,20),canSave=new Set([...ackP,...prints.map(p=>p.print_id)]),awards=printDB.awards.filter(a=>!ackA.has(awardId(a))&&canSave.has(a.print_id)).slice(0,200);if(!force&&!prints.length&&!awards.length)return;$(\'#historySync\').textContent=\'データを ほぞんしています…\';bridge(\'history_sync\',{history_token:historyToken,profile_id:portalProfile,prints,awards});}\nfunction onHistoryReply(result){if(result.profile_id&&result.profile_id!==portalProfile)return;if(result.ok&&result.history){const data=result.history;mergeHistory(data);printDB.ackedPrints=[...new Set([...printDB.ackedPrints,...data.acked_print_ids,...data.prints.map(p=>p.print_id)])];printDB.ackedAwards=[...new Set([...printDB.ackedAwards,...data.acked_award_ids,...data.awards.map(awardId)])];persistHistory();renderHistory();$(\'#historySync\').textContent=result.linked?\'ちょこまなにも ほぞんしました。\':\'データは ほぞんずみです。\';scheduleHistorySync()}else{$(\'#historySync\').textContent=result.code===\'not_configured\'?\'この端末に ほぞんしました。データベースの設定が必要です。\':\'この端末に ほぞんしました。つうしんが もどると じどうで ほぞんします。\';diag(\'history_sync_failed\',{code:result.code||\'error\'})}}\nfunction scheduleMidnight(){clearTimeout(midnightTimer);const shifted=new Date(Date.now()+9*3600000),next=Date.UTC(shifted.getUTCFullYear(),shifted.getUTCMonth(),shifted.getUTCDate()+1)-9*3600000;midnightTimer=setTimeout(()=>{renderHistory();scheduleMidnight()},Math.max(100,next-Date.now()+30));}\nfunction onPortalReply(result){\n portalReady=true;portalLinked=!!(result.ok&&result.linked);if(portalLinked)portalProfile=result.profile_id;\n if(portalLinked){\n  const old=printDB,oldKey=PRINT_DB_KEY,nextKey=LEGACY_PRINT_DB_KEY+\':profile:\'+portalProfile;\n  try{localStorage.setItem(LAST_PROFILE_KEY,portalProfile)}catch{}\n  PRINT_DB_KEY=nextKey;printDB={prints:[],awards:[],ackedPrints:[],ackedAwards:[]};mergeHistory(loadHistory());\n  if(result.adopt_legacy&&oldKey===LEGACY_PRINT_DB_KEY){mergeHistory(old);printDB.ackedPrints=[];printDB.ackedAwards=[];}\n  portalPlan=result.plan_launch||null;\n  if(portalPlan?.menu_id===\'moji.hiragana.handwriting\'||portalPlan?.menu_id===\'moji.katakana.handwriting\'){\n   script=portalPlan.menu_id.split(\'.\')[1];select(\'#scripts\',\'script\',script);update();\n  }\n  $(\'#portalStatus\').textContent=\'ちょこまなと つながっています。\';\n }else{\n  $(\'#portalStatus\').textContent=result.ok?\'この端末に ほぞんします。ちょこまなから ひらくと つながります。\':\'ちょこまなとの接続を確認できません。端末に記録を残します。\';\n }\n if(result.plan_notice)$(\'#portalStatus\').textContent=result.plan_notice;\n persistHistory();renderHistory();syncHistory(true);height();\n}\nfunction initHistory(){try{const last=localStorage.getItem(LAST_PROFILE_KEY);if(last&&/^[0-9a-f-]{36}$/i.test(last)){portalProfile=last;PRINT_DB_KEY=LEGACY_PRINT_DB_KEY+\':profile:\'+last}}catch{}try{historyToken=localStorage.getItem(PRINT_TOKEN_KEY)||\'\'}catch{}if(!/^[0-9a-f]{64}$/.test(historyToken)){const bytes=new Uint8Array(32);crypto.getRandomValues(bytes);historyToken=Array.from(bytes,b=>b.toString(16).padStart(2,\'0\')).join(\'\');try{localStorage.setItem(PRINT_TOKEN_KEY,historyToken)}catch{}}mergeHistory(loadHistory());renderHistory();setInterval(()=>{if(!portalLinked&&!active&&!queue.some(x=>x.kind===\'portal_hello\'))bridge(\'portal_hello\',{history_token:historyToken});else syncHistory(false)},30000);scheduleMidnight();window.addEventListener(\'storage\',e=>{if(e.key===PRINT_DB_KEY){mergeHistory(loadHistory());renderHistory()}});document.addEventListener(\'visibilitychange\',()=>{if(!document.hidden){renderHistory();syncHistory(true)}});}\n$(\'#mic\').onclick=()=>{if(micWanted){stopMic();return}retries=0;micRequestedAt=performance.now();micWanted=true;run++;$(\'#candidates\').textContent=\'\';micUI(\'マイクを じゅんびしています…\');listen(run)};\nfunction resetWord(){\n stopMic();clearTimeout(timer);timer=null;revision++;resetEpoch++;\n cancelTextRequest();pdfSnapshot=null;inputStartedAt=0;\n $(\'#word\').value=\'\';$(\'#candidates\').textContent=\'\';$(\'#preview\').textContent=\'\';\n $(\'#info\').textContent=\'\';$(\'#pdfStatus\').textContent=\'\';$(\'#pdf\').disabled=true;\n clearPreview();diag(\'word_reset\');\n}\n$(\'#resetWord\').onclick=resetWord;\n$(\'#makeTab\').onclick=()=>historyTab(false);$(\'#historyTab\').onclick=()=>{historyTab(true);syncHistory(true)};\n$(\'#word\').addEventListener(\'input\' ,()=>{inputStartedAt=performance.now();if(micWanted)stopMic();$(\'#candidates\').textContent=\'\';clearPreview();clearTimeout(timer);revision++;timer=setTimeout(update,220)});\n$$(\'[data-script]\').forEach(b=>b.onclick=()=>{script=b.dataset.script;select(\'#scripts\',\'script\',script);update()});\n$$(\'[data-dir]\').forEach(b=>b.onclick=()=>{orientation=b.dataset.dir;select(\'#directions\',\'dir\',orientation);if(word)draw()});\n$$(\'[data-mode]\').forEach(b=>b.onclick=()=>{mode=b.dataset.mode;select(\'#modes\',\'mode\',mode);update()});\n$(\'#pdf\').onclick=requestPDF;$(\'#logs\').onclick=()=>exportDiagnostics(\'json\');$(\'#logsCsv\').onclick=()=>exportDiagnostics(\'csv\');$(\'#settingsButton\').onclick=()=>showSettings(true);$(\'#settingsBack\').onclick=()=>showSettings(false);\nwindow.addEventListener(\'message\',e=>{if(e.source!==parent||e.data?.type!==\'streamlit:render\')return;const args=e.data.args||{};if(args.chokomana_home_url?.startsWith(\'https://\'))$(\'#hub\').href=args.chokomana_home_url;if(Number.isFinite(args.server_startup_ms))diag(\'server_component_ready\',{server_ms:args.server_startup_ms});onReply(args.result);height()});\nwindow.addEventListener(\'pagehide\',stopMic);document.addEventListener(\'visibilitychange\',()=>{if(document.hidden)stopMic()});window.addEventListener(\'error\',()=>diag(\'js_error\'));new ResizeObserver(height).observe($(\'#app\'));$$(\'details\').forEach(x=>x.addEventListener(\'toggle\',height));\nparent.postMessage({isStreamlitMessage:true,type:\'streamlit:componentReady\',apiVersion:1},\'*\');diag(\'startup\');initDiagnostics();height();\ninitHistory();bridge(\'portal_hello\',{history_token:historyToken});\n\n// Chokomana Mimamori: authenticated voice and stamp channel.\nfunction cmWatchWidget(adapter){\n const API=\'https://keewulninyoeylgflshb.supabase.co/functions/v1/chokomana-watch\';\n let panel=null,launcher=null,rows=[],busy=false,recording=null,media=null,blob=null,audioURL=null,timer=null,requestId=null,signature=\'\',seen=new Set(),lastProof=null;\n const make=(tag,text,cls)=>{const n=document.createElement(tag);if(text)n.textContent=text;if(cls)n.className=cls;return n};\n const ready=()=>{try{return !!adapter.ready()&&/^[a-f0-9]{64}$/.test(adapter.proof()||\'\')}catch{return false}};\n async function api(action,data={}){if(!ready())throw new Error(\'ちょこまな本体から ひらいてね。\');const r=await fetch(API,{method:\'POST\',headers:{\'Content-Type\':\'application/json\',\'X-CM-Child\':adapter.proof()},body:JSON.stringify({action,source_app:adapter.source,...data}),signal:AbortSignal.timeout(25000)});const out=await r.json();if(!r.ok)throw new Error(out.error||\'つうしんを かくにんしてね。\');return out}\n function status(text){if(panel)panel.querySelector(\'.cmw-status\').textContent=text}\n function stop(){clearTimeout(timer);timer=null;if(recording?.state===\'recording\')recording.stop();media?.getTracks().forEach(t=>t.stop());media=null;if(panel)panel.querySelector(\'.cmw-record\').textContent=\'● こえを ろくおん\'}\n function discard(){blob=null;requestId=null;if(audioURL)URL.revokeObjectURL(audioURL);audioURL=null;const preview=panel?.querySelector(\'.cmw-preview\');if(preview){preview.pause();preview.removeAttribute(\'src\');preview.hidden=true}panel?.querySelector(\'.cmw-send\').setAttribute(\'hidden\',\'\');}\n async function record(){if(recording?.state===\'recording\'){stop();return}try{if(!navigator.mediaDevices?.getUserMedia||typeof MediaRecorder===\'undefined\')throw new Error(\'このブラウザでは ろくおんできません。\');discard();media=await navigator.mediaDevices.getUserMedia({audio:true});const mime=[\'audio/webm;codecs=opus\',\'audio/mp4\',\'audio/ogg;codecs=opus\'].find(m=>MediaRecorder.isTypeSupported(m));recording=new MediaRecorder(media,mime?{mimeType:mime}:undefined);const chunks=[];recording.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};recording.onstop=()=>{blob=new Blob(chunks,{type:recording.mimeType||mime||\'audio/webm\'});requestId=crypto.randomUUID();audioURL=URL.createObjectURL(blob);const p=panel.querySelector(\'.cmw-preview\');p.src=audioURL;p.hidden=false;panel.querySelector(\'.cmw-send\').hidden=false;status(\'こえを かくにんして、おくってね。\')};recording.start();panel.querySelector(\'.cmw-record\').textContent=\'■ ろくおんを とめる\';status(\'ろくおんちゅう：30びょうまで\');timer=setTimeout(stop,30000)}catch(e){stop();status(e.name===\'NotAllowedError\'?\'マイクを きょかしてね。\':e.message)}}\n async function send(){const b=panel.querySelector(\'.cmw-send\');b.disabled=true;try{if(!blob)throw new Error(\'こえを ろくおんしてね。\');if(blob.size>3145728)throw new Error(\'もうすこし みじかく ろくおんしてね。\');const audio=await new Promise((resolve,reject)=>{const f=new FileReader();f.onload=()=>resolve(String(f.result).split(\',\')[1]);f.onerror=reject;f.readAsDataURL(blob)});await api(\'child_send\',{kind:\'voice\',audio,mime:blob.type,request_id:requestId});discard();status(\'こえを おくったよ！\');await poll()}catch(e){status(e.message)}finally{b.disabled=false}}\n function build(){if(panel)return;const style=make(\'style\');style.textContent=`\n .cmw-launch{position:fixed!important;right:10px!important;top:10px!important;width:40px!important;height:40px!important;min-height:40px!important;padding:0!important;border-radius:50%!important;border:2px solid #fff!important;background:#247968!important;color:white!important;font-size:22px!important;z-index:2147482000!important;box-shadow:0 3px 10px #0002!important}.cmw-launch.cmw-unread:after{content:\'\';position:absolute;right:-1px;top:-1px;width:11px;height:11px;background:#e6a457;border:2px solid white;border-radius:50%}\n .cmw-panel{position:fixed!important;right:9px!important;top:58px!important;width:min(350px,calc(100vw - 18px))!important;max-height:min(75vh,580px)!important;overflow:auto!important;background:white!important;color:#263f3a!important;border:1px solid #d5e2d6!important;border-radius:18px!important;padding:18px!important;z-index:2147482001!important;box-shadow:0 12px 50px #172e3833!important;font:14px/1.8 system-ui,sans-serif!important}.cmw-panel[hidden]{display:none!important}.cmw-panel button{font:inherit!important;min-height:40px!important;border-radius:10px!important;border:1px solid #dce6dc!important;padding:7px 12px!important;color:#247968!important;background:#eff5ed!important;margin:5px 5px 5px 0!important}.cmw-panel button:disabled{opacity:.5}.cmw-panel h3{font-size:16px!important;color:#263f3a!important;margin:0 0 5px!important}.cmw-panel audio{max-width:100%!important;width:100%!important;height:38px!important}.cmw-panel audio[hidden],.cmw-panel button[hidden]{display:none!important}.cmw-status{font-size:11px!important;color:#667d69!important;margin:7px 0!important}.cmw-row{border-top:1px solid #e4eae1;padding:10px 0}.cmw-row small{font-size:10px!important;color:#74846e!important}.cmw-stamp{font-size:21px!important;color:#247968!important;margin:9px 0!important}.cmw-pop{position:fixed!important;top:57px!important;right:9px!important;max-width:260px!important;padding:12px 18px!important;background:#eff6ea!important;color:#247968!important;box-shadow:0 4px 20px #0002!important;border-radius:16px!important;font:600 15px/1.6 system-ui!important;z-index:2147481999!important}.cmw-panel :focus-visible,.cmw-launch:focus-visible{outline:3px solid #e0a15d!important;outline-offset:2px!important}\n .cmw-medals{margin:10px 0;padding:12px!important;border-radius:13px;background:#f7faf1!important;border:1px solid #e2e9d8}.cmw-medals>strong{display:block;font-size:14px!important;color:#247968!important}.cmw-medals>small{display:block!important}.cmw-medal-condition{border-top:1px solid #dfe8d6;margin-top:10px;padding-top:10px}.cmw-condition-state{display:inline-block;font-size:10px!important;color:#7d8b70!important}.cmw-complete .cmw-condition-state{color:#247968!important;background:#e0efdb;padding:1px 6px;border-radius:8px}.cmw-condition-title{font-size:12px!important;font-weight:600!important;overflow-wrap:anywhere}.cmw-needed{font-size:14px!important;margin:3px 0}.cmw-acquired{display:flex;gap:6px;flex-wrap:wrap;margin:5px 0}.cmw-earned{font-size:19px!important;padding:2px 8px;background:#fff;border:1px solid #e0e8d8;border-radius:12px}.cmw-no-medal{font-size:12px;color:#829178}.cmw-progress-track{height:7px;background:#e2e9d9;border-radius:8px;margin:8px 0 4px;overflow:hidden}.cmw-progress-track>i{display:block;height:100%;background:#6caa69;border-radius:8px}\n`;document.head.append(style);\n launcher=make(\'button\',\'♬\',\'cmw-launch\');launcher.title=\'おうちと おはなし\';launcher.setAttribute(\'aria-label\',\'おうちと おはなし\');launcher.type=\'button\';document.body.append(launcher);\n panel=make(\'section\',null,\'cmw-panel\');panel.hidden=true;panel.setAttribute(\'aria-label\',\'おうちと おはなし\');panel.append(make(\'h3\',\'おうちと おはなし\'));const close=make(\'button\',\'とじる\');close.style.float=\'right\';close.onclick=()=>{stop();panel.hidden=true;adapter.resize?.(false);launcher.focus()};panel.prepend(close);\n const recordBtn=make(\'button\',\'● こえを ろくおん\',\'cmw-record\');recordBtn.onclick=record;const preview=make(\'audio\',null,\'cmw-preview\');preview.controls=true;preview.hidden=true;const sendBtn=make(\'button\',\'このこえを おくる\',\'cmw-send\');sendBtn.hidden=true;sendBtn.onclick=send;const retry=make(\'button\',\'やりなおす\');retry.onclick=()=>{stop();discard();status(\'こえを ろくおんしてね。\')};panel.append(recordBtn,preview,sendBtn,retry,make(\'p\',\'こえは 30びょうまで おくれるよ。\',\'cmw-status\'),make(\'div\',null,\'cmw-list\'));document.body.append(panel);\n launcher.onclick=async()=>{panel.hidden=!panel.hidden;adapter.resize?.(!panel.hidden);if(!panel.hidden){render();for(const m of rows.filter(m=>m.sender===\'parent\'&&!m.read_at))api(\'child_read\',{id:m.id}).catch(()=>{});launcher.classList.remove(\'cmw-unread\');await poll()}else stop()};\n }\n function medalProgress(m){\n  if(!m.unlock_requirements?.length)return null;\n  const u=m.unlock||{unlocked:false,conditions:m.unlock_requirements},box=make(\'section\',null,\'cmw-medals\'),icons={gold:\'🥇\',silver:\'🥈\',bronze:\'🥉\'},names={gold:\'きん\',silver:\'ぎん\',bronze:\'どう\'};\n  box.setAttribute(\'aria-label\',\'このおたよりを きくための メダル\');\n  box.append(make(\'strong\',u.unlocked?\'🔓 ぜんぶ そろった！ こえを きけるよ\':\'🔒 メダルを そろえて ひらこう\'));\n  box.append(make(\'small\',\'おたよりが とどいてからの メダルを かぞえるよ。\'));\n  for(const r of u.conditions){const line=make(\'div\',null,\'cmw-medal-condition\'+(r.complete?\' cmw-complete\':\'\'));line.append(make(\'span\',r.complete?\'✓ できた！\':\'○ あつめよう\',\'cmw-condition-state\'),make(\'div\',r.label,\'cmw-condition-title\'));\n   line.append(make(\'div\',icons[r.grade]+\' \'+names[r.grade]+(r.comparison===\'at_least\'&&r.grade!==\'gold\'?\' いじょう\':\'\')+\'：\'+r.count+\'こ\',\'cmw-needed\'));\n   const acquired=make(\'div\',null,\'cmw-acquired\');for(const e of r.earned||[])acquired.append(make(\'span\',icons[e.grade]+\' \'+e.count+\'こ\',\'cmw-earned\'));if(!r.earned?.length)acquired.append(make(\'span\',\'まだ ないよ\',\'cmw-no-medal\'));line.append(acquired);\n   const meter=make(\'div\',null,\'cmw-progress-track\'),fill=make(\'i\');fill.style.width=Math.min(100,100*(r.qualified_count||0)/r.count)+\'%\';meter.append(fill);meter.setAttribute(\'role\',\'progressbar\');meter.setAttribute(\'aria-label\',r.label+\'のメダル\');meter.setAttribute(\'aria-valuemin\',\'0\');meter.setAttribute(\'aria-valuemax\',String(r.count));meter.setAttribute(\'aria-valuenow\',String(Math.min(r.count,r.qualified_count||0)));line.append(meter,make(\'small\',(r.qualified_count||0)+\' / \'+r.count+\'こ\'+(r.complete?\'：クリア！\':\'：あと \'+(r.remaining??r.count)+\'こ\')));box.append(line);\n  }return box;\n }\n function render(){if(!panel||panel.hidden)return;const list=panel.querySelector(\'.cmw-list\');if([...list.querySelectorAll(\'audio\')].some(a=>!a.paused))return;const next=JSON.stringify(rows);if(next===signature)return;signature=next;list.replaceChildren();if(!rows.length)list.append(make(\'p\',\'まだ おたよりは ないよ。\',\'cmw-status\'));\n for(const m of rows){const row=make(\'div\',null,\'cmw-row\');const date=new Intl.DateTimeFormat(\'ja-JP\',{timeZone:\'Asia/Tokyo\',month:\'numeric\',day:\'numeric\',hour:\'2-digit\',minute:\'2-digit\'}).format(new Date(m.created_at));if(m.kind===\'voice\')row.append(make(\'h3\',m.title||\'こえの おたより\'));row.append(make(\'small\',(m.sender===\'parent\'?\'おうちから\':\'じぶんから\')+\' · \'+date));\n if(m.kind===\'stamp\')row.append(make(\'div\',\'🌸 \'+m.stamp,\'cmw-stamp\'));else{const locked=m.sender===\'parent\'&&m.unlock?.unlocked===false,progress=medalProgress(m);if(progress)row.append(progress);const play=make(\'button\',locked?\'🔒 メダルを そろえよう\':\'▶ こえを きく\');play.disabled=locked;play.onclick=async()=>{play.disabled=true;try{const r=await api(\'child_audio\',{id:m.id});const a=make(\'audio\');a.controls=true;a.src=r.url;a.onended=()=>{if(m.sender===\'parent\')api(\'child_played\',{id:m.id}).then(poll).catch(e=>status(e.message))};play.replaceWith(a);await a.play();if(m.sender===\'parent\')await api(\'child_read\',{id:m.id})}catch(e){status(e.message);play.disabled=false}};row.append(play)}row.append(make(\'small\',m.played_at?\'さいせいずみ\':m.read_at?\'よんだよ\':m.sender===\'child\'?\'まだ よんでいないよ\':\'\'));list.append(row)}\n }\n async function poll(){if(busy||document.hidden||!ready())return;busy=true;try{build();if(lastProof!==adapter.proof()){lastProof=adapter.proof();seen=new Set();rows=[];signature=\'\';const key=\'cm_watch_open:\'+adapter.source+\':\'+lastProof.slice(0,12);let v;try{v=JSON.parse(sessionStorage.getItem(key)||\'null\')}catch{}if(!v||Date.now()-v.at>1800000){v={id:crypto.randomUUID(),at:Date.now()};await api(\'child_hello\',{request_id:v.id});try{sessionStorage.setItem(key,JSON.stringify(v))}catch{}}}\n const r=await api(\'child_poll\');rows=r.messages;const incoming=rows.filter(m=>m.sender===\'parent\'&&!m.read_at);launcher.classList.toggle(\'cmw-unread\',incoming.length>0);for(const m of incoming){if(seen.has(m.id))continue;seen.add(m.id);const p=make(\'div\',m.kind===\'stamp\'?\'🌸 \'+m.stamp:(m.unlock?.unlocked===false?\'🔒 ひみつの こえが とどいたよ！\':\'♬ おうちから こえが とどいたよ！\'),\'cmw-pop\');document.body.append(p);setTimeout(()=>p.remove(),6000)}render();if(!panel.hidden)for(const m of incoming)api(\'child_read\',{id:m.id}).catch(()=>{});\n }catch(e){lastProof=null;status(e.message);if(launcher)launcher.title=\'おうちと おはなし：つうしんを かくにんちゅう\'}finally{busy=false}}\n setInterval(poll,12000);setTimeout(poll,1800);document.addEventListener(\'visibilitychange\',()=>{if(document.hidden)stop();else poll()});\n return {poll};\n}\n\ncmWatchWidget({source:\'moji\',proof:()=>historyToken,ready:()=>portalReady&&portalLinked});\n</script></body></html>\n'

def frontend_html():
    return HTML_TEMPLATE

def process_component_delivery(st,message):
    """Replay an ACK for a retried UUID without repeating its writes."""
    delivery=message.get("delivery_id")
    if not isinstance(delivery,str):return False
    transport=(delivery,message.get("transport_sequence",0))
    if transport==st.session_state.get("_kana_last_transport"):return False
    cache=st.session_state.setdefault("_kana_reply_cache",{})
    if delivery in cache:result=cache[delivery]
    else:
        start=time.perf_counter()
        result=process_message(st,message)
        result["server_ms"]=round((time.perf_counter()-start)*1000,2)
        cache[delivery]=result
        while len(cache)>32:del cache[next(iter(cache))]
    st.session_state["_kana_last_result"]=result
    st.session_state["_kana_last_delivery"]=delivery
    st.session_state["_kana_last_transport"]=transport
    return True


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
     .preview.page{position:relative;aspect-ratio:210/297;padding:0;container-type:inline-size;overflow:hidden}.page .cell{font-size:6.83cqw}.preview-pages{width:100%}
</style>""",unsafe_allow_html=True)
    # Stable single HTML component; Python side does no Supabase I/O on the startup path.
    @st.cache_resource(show_spinner=False)
    def get_component(html: str):
        digest=hashlib.sha256(html.encode()).hexdigest()[:16]
        directory=Path(tempfile.gettempdir())/("chokomana_kana_component_"+digest)
        directory.mkdir(parents=True,exist_ok=True)
        index=directory/"index.html"
        if not index.exists() or index.stat().st_size!=len(html.encode()):index.write_text(html,encoding="utf-8")
        return components.declare_component("chokomana_kana_"+digest,path=str(directory))
    component=get_component(frontend_html())
    try:
        hub=_safe_https_url(str(st.secrets.get("CHOKOMANA_HOME_URL",DEFAULT_HUB_URL))) or DEFAULT_HUB_URL
    except Exception:hub=DEFAULT_HUB_URL
    ticket=st.query_params.get('cm_launch')
    if ticket:
        fingerprint=hashlib.sha256(str(ticket).encode()).hexdigest()
        if fingerprint!=st.session_state.get('_moji_launch_instance'):
            for name in ('_moji_identity','_kana_last_result','_kana_last_delivery','_kana_last_transport','_kana_reply_cache'):
                st.session_state.pop(name,None)
            st.session_state['_moji_ticket']=str(ticket)
            st.session_state['_moji_launch_instance']=fingerprint
    last_result=st.session_state.get("_kana_last_result",None)
    message=component(result=last_result,chokomana_home_url=hub,
       server_startup_ms=round((time.perf_counter()-run_t0)*1000,2),default=None,key="kana_client_"+st.session_state.get("_moji_launch_instance","standalone")[:16])
    if isinstance(message,dict) and process_component_delivery(st,message):
        st.rerun()

if __name__=="__main__":
    _run_streamlit()
