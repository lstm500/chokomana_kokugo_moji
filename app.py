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


def cm_graduation_snapshot(rows, profile, source):
    if not isinstance(rows, list) or len(rows) > 1:
        raise ValueError('invalid graduation state')
    if not rows:
        return {}
    row = rows[0]
    if str(row.get('profile_id')) != str(profile) or row.get('source_app') != source:
        raise ValueError('graduation owner mismatch')
    items = row.get('items')
    if not isinstance(items, dict):
        raise ValueError('invalid graduation items')
    return {key: item for key, item in items.items()
            if key.startswith(('unit:', 'question:')) and isinstance(item, dict)}


def cm_poll_graduation(st, state, profile, source, read_rows):
    # The profile comes from the server-verified link, never the browser message.
    if state.get('_cm_graduation_owner') != profile:
        state['_cm_graduation_owner'] = profile
        state['_cm_graduation_units'] = []
        state.pop('_cm_graduation_checked', None)
    if not profile:
        return
    import time
    if time.monotonic() - state.get('_cm_graduation_checked', -15) < 14:
        return
    state['_cm_graduation_checked'] = time.monotonic()
    try:
        items = cm_graduation_snapshot(read_rows(), profile, source)
    except Exception as exc:
        import logging
        logging.getLogger('chokomana_graduation').warning('%s graduation read failed (%s)', source, type(exc).__name__)
        return
    units = sorted(key[5:] for key, item in items.items()
                   if key.startswith('unit:') and isinstance(item.get('record'), dict))
    if units != state['_cm_graduation_units']:
        state['_cm_graduation_units'] = units
        st.rerun()


def cm_moji_graduation_rows(secrets, profile):
    from urllib.parse import urlencode
    url, key = _moji_config(secrets)
    params = urlencode({'profile_id': 'eq.' + profile, 'source_app': 'eq.moji',
                        'select': 'profile_id,source_app,items', 'limit': '1'})
    headers = {'apikey': key}
    if not key.startswith('sb_secret_'):
        headers['Authorization'] = 'Bearer ' + key
    with urlopen(Request(url + '/rest/v1/chokomana_graduation_states?' + params, headers=headers), timeout=6) as response:
        return json.load(response)


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
    profile=(st.session_state.get('_moji_identity') or {}).get('profile_id')
    @st.fragment(run_every=15)
    def check_graduation():
        cm_poll_graduation(st, st.session_state, profile, 'moji',
                           lambda: cm_moji_graduation_rows(st.secrets, profile))
    check_graduation()
    last_result=st.session_state.get("_kana_last_result",None)
    message=component(hub_speech_speed=st.query_params.get('cm_speech_speed'),result=last_result,chokomana_home_url=hub,
       graduated_units=st.session_state.get('_cm_graduation_units', []),
       server_startup_ms=round((time.perf_counter()-run_t0)*1000,2),default=None,key="kana_client_"+st.session_state.get("_moji_launch_instance","standalone")[:16])
    if isinstance(message,dict) and process_component_delivery(st,message):
        st.rerun()

if __name__=="__main__":
    _run_streamlit()
