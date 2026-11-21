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
    message=component(result=last_result,chokomana_home_url=hub,
       graduated_units=st.session_state.get('_cm_graduation_units', []),
       server_startup_ms=round((time.perf_counter()-run_t0)*1000,2),default=None,key="kana_client_"+st.session_state.get("_moji_launch_instance","standalone")[:16])
    if isinstance(message,dict) and process_component_delivery(st,message):
        st.rerun()

if __name__=="__main__":
    _run_streamlit()
