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


HTML_TEMPLATE = __import__('zlib').decompress(__import__('base64').b64decode('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')).decode('utf-8')

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
