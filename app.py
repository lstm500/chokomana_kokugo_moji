# -*- coding: utf-8 -*-
"""ちょこまな ひらがな・カタカナ | Streamlit + local-first JS + Supabase.

CLI without Streamlit:
  python app.py --chokomana-sql
  python app.py --chokomana-catalog
  python app.py --sample-pdf /tmp/kana.pdf
  python app.py --self-test

No database is changed by the CLI or by starting the app. Shared database
migrations must be applied separately, and the independent chokomana hub must
be taught how to issue source_app=kana tickets before a real link is possible.
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

APP_VERSION = "kana-1.0.0"
CONTENT_VERSION = "kana-material-1"
SOURCE_APP = "kana"
DEFAULT_HUB_URL = "https://chokomana-hkgx4uyxxezjtr2sj35jwj.streamlit.app/"
MAX_WORKSHEET_CHARS = 60

# Fixed educational content only. Similarity/novelty is computed from real IDs.
SEION = [
    ("あ","ア"),("い","イ"),("う","ウ"),("え","エ"),("お","オ"),
    ("か","カ"),("き","キ"),("く","ク"),("け","ケ"),("こ","コ"),
    ("さ","サ"),("し","シ"),("す","ス"),("せ","セ"),("そ","ソ"),
    ("た","タ"),("ち","チ"),("つ","ツ"),("て","テ"),("と","ト"),
    ("な","ナ"),("に","ニ"),("ぬ","ヌ"),("ね","ネ"),("の","ノ"),
    ("は","ハ"),("ひ","ヒ"),("ふ","フ"),("へ","ヘ"),("ほ","ホ"),
    ("ま","マ"),("み","ミ"),("む","ム"),("め","メ"),("も","モ"),
    ("や","ヤ"),("ゆ","ユ"),("よ","ヨ"),
    ("ら","ラ"),("り","リ"),("る","ル"),("れ","レ"),("ろ","ロ"),
    ("わ","ワ"),("を","ヲ"),("ん","ン"),
]
ADVANCED = [
    ("が","ガ"),("ぎ","ギ"),("ぐ","グ"),("げ","ゲ"),("ご","ゴ"),
    ("ざ","ザ"),("じ","ジ"),("ず","ズ"),("ぜ","ゼ"),("ぞ","ゾ"),
    ("だ","ダ"),("ぢ","ヂ"),("づ","ヅ"),("で","デ"),("ど","ド"),
    ("ば","バ"),("び","ビ"),("ぶ","ブ"),("べ","ベ"),("ぼ","ボ"),
    ("ぱ","パ"),("ぴ","ピ"),("ぷ","プ"),("ぺ","ペ"),("ぽ","ポ"),
    ("きゃ","キャ"),("きゅ","キュ"),("きょ","キョ"),
    ("しゃ","シャ"),("しゅ","シュ"),("しょ","ショ"),
    ("ちゃ","チャ"),("ちゅ","チュ"),("ちょ","チョ"),
    ("にゃ","ニャ"),("にゅ","ニュ"),("にょ","ニョ"),
    ("りゃ","リャ"),("りゅ","リュ"),("りょ","リョ"),
]
WORDS_BASIC = [
    "りんご","みかん","いぬ","ねこ","くるま","でんしゃ","はな","そら",
    "うみ","やま","あめ","ゆき","かさ","くつ","ほん","とけい",
    "さかな","たまご","うさぎ","きつね","たぬき","こあら","すいか","ばなな",
    "ぱんだ","おにぎり","えんぴつ","ふうせん","ひこうき","ふね",
]
WORDS_ADVANCED = [
    "しんかんせん","しょうぼうしゃ","きょうりゅう","ちょうちょ","じてんしゃ",
    "びょういん","こうえん","すべりだい","かいだん","でんき","ぷりん",
    "けいたい","ぺんぎん","かめら","らいおん","ぶどう","ぴあの",
    "こんぴゅーたー","しゃしん","きゅうり","おうだんほどう","きゅうきゅうしゃ",
    "あいすくりーむ","ちょこれーと","たんじょうび","はっぴょう",
]


def to_katakana(text: str) -> str:
    return "".join(chr(ord(x) + 96) if "ぁ" <= x <= "ゖ" else x for x in text)


def to_hiragana(text: str) -> str:
    return "".join(chr(ord(x) - 96) if "ァ" <= x <= "ヶ" else x for x in text)


MENUS = []
for script in ("hiragana", "katakana"):
    for level in (1, 2):
        for subject in ("letters", "words"):
            title = ("ひらがな" if script == "hiragana" else "カタカナ")
            skill = ("もじ" if subject == "letters" else "ことば")
            count = 10 if subject == "letters" else 6
            menu_id = f"kana:{script}:{subject}:{level}"
            MENUS.append({
                "menu_id": menu_id, "script": script, "subject": subject,
                "level": level, "label": f"{title}・{skill}・{'初級' if level == 1 else '中級'}",
                "category_id": script, "category_label": title,
                "difficulty_id": str(level), "difficulty_order": level,
                "difficulty_label": "初級" if level == 1 else "中級",
                "progression_group_id": f"kana:{script}:{subject}",
                "skill_ids": [f"kana:{script}:{subject}"],
                "question_count_default": count,
                "estimated_duration_ms": (20000 if subject == "letters" else 32000) * count,
                "next_menu_ids": ([f"kana:{script}:{subject}:{level+1}"] if level == 1 else []),
            })
MENU_BY_ID = {m["menu_id"]: m for m in MENUS}


def build_question_bank():
    bank = {}
    for m in MENUS:
        script,subject,level = m["script"],m["subject"],m["level"]
        if subject == "letters":
            entries = SEION if level == 1 else ADVANCED
            sources = [a if script == "katakana" else b for a,b in entries]
            targets = [b if script == "katakana" else a for a,b in entries]
        else:
            sources = WORDS_BASIC if level == 1 else WORDS_ADVANCED
            sources = [s if script == "katakana" else to_katakana(s) for s in sources]
            targets = [to_katakana(s) if script == "katakana" else s for s in (WORDS_BASIC if level == 1 else WORDS_ADVANCED)]
        for index,(src,correct) in enumerate(zip(sources, targets)):
            qid = f"{m['menu_id']}:q{index+1:03d}"
            bank[qid] = {
                "question_id":qid,"menu_id":m["menu_id"],"prompt":src,
                "answer":correct,"pool":targets,
                "family_id":m["progression_group_id"],"skill_ids":m["skill_ids"],
            }
    return bank

QUESTION_BANK = build_question_bank()


def catalog_rows(base_url: str = "") -> list[dict]:
    rows=[]
    valid_url = _safe_https_url(base_url)
    for m in MENUS:
        payload = {
            "menu_id": m["menu_id"], "label":m["label"],
            "category_id":m["category_id"],"category_label":m["category_label"],
            "difficulty_id":m["difficulty_id"],"difficulty_label":m["difficulty_label"],
            "difficulty_order":m["difficulty_order"],
            "progression_group_id":m["progression_group_id"],
            "skill_ids":m["skill_ids"],"available_skill_ids":m["skill_ids"],
            "prerequisite_skill_ids":None,"mode_options":["challenge"],
            "question_count_default":m["question_count_default"],
            "question_count_options":[m["question_count_default"]],
            "available_question_count":sum(q["menu_id"]==m["menu_id"] for q in QUESTION_BANK.values()),
            "estimated_duration_ms":m["estimated_duration_ms"],
            "duration_basis":"provisional","duration_sample_count":0,
            "duration_estimate_note":"仮設定。文字は1問20秒、ことばは1問32秒。実測の中央値3件以上があれば置換する。",
            "age_min":None,"age_max":None,"age_basis":None,
            "launch_url":valid_url,"supports_direct_launch":True,
            "selection_instructions":["チャレンジ",m["label"]],
            "is_active":True,"next_menu_ids":m["next_menu_ids"],
            "assessment_method":"automatic", "score_rules": {"gold":1.0,"silver":0.8,"bronze":0.6},
        }
        rows.append({"schema_version":1,"source_app":SOURCE_APP,"entry_type":"menu", "entry_id":m["menu_id"],"content_version":CONTENT_VERSION,"payload":payload})
    skills={}
    for m in MENUS:
        sid=m["skill_ids"][0]
        skills[sid]={
            "skill_id":sid,"label":m["category_label"]+("・もじ" if m["subject"]=="letters" else "・ことば"),
            "parent_skill_id":None,"category_id":m["category_id"],
            "definition":"別の文字体系に示した同じ読みを、四択から選択する課題。手書きの上達は評価しない。",
            "observation_rule":"最初の自動採点回答のみ初回正答率に含め、ヒント、再回答、別日での正答を区別する。",
            "prerequisite_skill_ids":None,
            "progression_group_id":m["progression_group_id"],"difficulty_axes":["difficulty_id"],"is_active":True,
        }
    rows.extend({"schema_version":1,"source_app":SOURCE_APP,"entry_type":"skill","entry_id":sid,
                  "content_version":CONTENT_VERSION,"payload":p} for sid,p in skills.items())
    return rows


def _safe_https_url(value: str) -> str:
    if not value:
        return ""
    p=urlsplit(value)
    return value.strip().rstrip("/")+"/" if p.scheme=="https" and p.netloc and not p.query and not p.fragment and not p.username else ""


def question_catalog_rows(ids: list[str]) -> list[dict]:
    rows=[]
    for qid in dict.fromkeys(ids):
        q=QUESTION_BANK.get(qid)
        if q is None:continue
        menu=MENU_BY_ID[q["menu_id"]]
        rows.append({"schema_version":1,"source_app":SOURCE_APP,"entry_type":"question", "entry_id":qid,
            "content_version":CONTENT_VERSION,"payload":{
                "question_id":qid,"question_family_id":q["family_id"],"menu_ids":[q["menu_id"]],
                "skill_ids":q["skill_ids"],"difficulty_id":menu["difficulty_id"],
                "content_hash":hashlib.sha256(json.dumps([q["prompt"],q["answer"]],ensure_ascii=False).encode()).hexdigest(),
                "assessment_method":"automatic","response_format":"choice", "correct_answer_spec":{"answer":q["answer"]},
                "choice_count":4,"is_generated":False,"is_active":True,
            }})
    return rows


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
            pdf.drawCentredString(x+cell/2,y+cell*.21,ch)

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


SQL = r"""-- ちょこまな共通テーブルに新しい source_app=kana を追加するための差分。
-- 既存のテーブルやデータを削除・再作成しない。
-- 実行前にDBの状態を確認し、既存制約が違う場合は中止してください。
begin;
-- Preserve all allowed source_app values in the current database, including english.
alter table public.chokomana_launch_tickets
  drop constraint if exists chokomana_launch_tickets_source_app_check;
alter table public.chokomana_launch_tickets
  add constraint chokomana_launch_tickets_source_app_check
  check (source_app in ('kotoba','soroban','geometry','english','kana'));
alter table public.chokomana_device_links
  drop constraint if exists chokomana_device_links_source_app_check;
alter table public.chokomana_device_links
  add constraint chokomana_device_links_source_app_check
  check (source_app in ('kotoba','soroban','geometry','english','kana'));

-- Existing RLS, grants, indexes and tables are retained. Service-role-only RPC.
create or replace function public.chokomana_append_kana_events_v1(
    p_device_hash text, p_events jsonb
) returns jsonb language plpgsql security invoker set search_path = '' as $$
declare
  owner_id text; item jsonb; record_row public.chokomana_learning_events;
  prior jsonb; expected jsonb; ids jsonb := '[]'::jsonb;
begin
  if p_device_hash !~ '^[a-f0-9]{64}$' then raise exception 'invalid_device'; end if;
  select profile_id::text into owner_id
    from public.chokomana_device_links
    where device_hash=p_device_hash and source_app='kana';
  if owner_id is null then raise exception 'unverified_profile' using errcode='42501'; end if;
  if jsonb_typeof(p_events) is distinct from 'array' or jsonb_array_length(p_events)>50
       or pg_column_size(p_events)>204800 then raise exception 'invalid_batch'; end if;
  for item in select value from jsonb_array_elements(p_events) loop
    if item->>'source_app' is distinct from 'kana'
       or item->>'profile_id' is distinct from owner_id
       or item->>'schema_version' is distinct from '1'
       or coalesce(item->>'event_hash','') !~ '^[a-f0-9]{64}$' then
      raise exception 'event_owner_or_schema_mismatch' using errcode='42501';
    end if;
    record_row:=jsonb_populate_record(null::public.chokomana_learning_events,item);
    record_row.received_at:=now();
    expected:=to_jsonb(record_row)-'received_at';
    select to_jsonb(e)-'received_at' into prior
      from public.chokomana_learning_events e where e.event_id=record_row.event_id;
    if prior is not null and prior is distinct from expected then
      raise exception 'event_id_content_conflict' using errcode='23505';
    end if;
    if prior is null then
      insert into public.chokomana_learning_events select record_row.*
        on conflict(event_id) do nothing;
      select to_jsonb(e)-'received_at' into prior
        from public.chokomana_learning_events e where e.event_id=record_row.event_id;
      if prior is distinct from expected then
        raise exception 'event_id_content_conflict' using errcode='23505';
      end if;
    end if;
    ids:=ids||jsonb_build_array(record_row.event_id);
  end loop;
  return jsonb_build_object('acked',ids);
end $$;
revoke all on function public.chokomana_append_kana_events_v1(text,jsonb) from PUBLIC,anon,authenticated;
grant execute on function public.chokomana_append_kana_events_v1(text,jsonb) to service_role;
-- Explicit GRANT so newly configured Data API permission changes cannot break the app.
grant select,insert,update on table public.chokomana_device_links to service_role;
grant select,update on table public.chokomana_launch_tickets to service_role;
grant select,insert on table public.chokomana_learning_catalog to service_role;
grant select,insert on table public.chokomana_learning_events to service_role;
grant select,insert on table public.chokomana_stamps to service_role;
commit;
"""


class SyncError(Exception):
    pass


def _config(secrets):
    url=str(secrets.get("SUPABASE_URL","")).rstrip("/")
    key=str(secrets.get("SUPABASE_SERVICE_ROLE_KEY",secrets.get("SUPABASE_SECRET_KEY","")))
    if urlsplit(url).scheme!="https" or not urlsplit(url).netloc or not key:raise SyncError("missing_secrets")
    return url,key


def _request(secrets,method,path,payload=None,params=None,prefer=None,timeout=5):
    url,key=_config(secrets)
    head={"apikey":key,"Content-Type":"application/json"}
    if not key.startswith("sb_secret_"):head["Authorization"]="Bearer "+key
    if prefer:head["Prefer"]=prefer
    query=("?"+urlencode(params)) if params else ""
    body=json.dumps(payload,ensure_ascii=False,separators=(",",":")).encode() if payload is not None else None
    with urlopen(Request(url+path+query,data=body,headers=head,method=method),timeout=timeout) as r:
        payload=r.read();return json.loads(payload) if payload else None


def _device_hash(token:str) -> str:
    if not isinstance(token,str) or re.fullmatch("[a-f0-9]{64}",token) is None:raise SyncError("invalid_device")
    return hashlib.sha256((SOURCE_APP+":"+token).encode()).hexdigest()


def _valid_uuid(value) -> str:
    try:return str(uuid.UUID(str(value)))
    except Exception as exc:raise SyncError("invalid_id") from exc


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00","Z")


def resolve_identity(secrets,token:str,ticket:str | None=None) -> str:
    device_hash=_device_hash(token)
    if ticket:
        if not re.fullmatch(r"[A-Za-z0-9_-]{32,256}",ticket):raise SyncError("invalid_ticket")
        # Atomic consume; a used/expired ticket cannot relink another device.
        rows=_request(secrets,"PATCH","/rest/v1/chokomana_launch_tickets",{"used_at":_now()},
           {"ticket_hash":"eq."+hashlib.sha256(ticket.encode()).hexdigest(),
            "source_app":"eq.kana","used_at":"is.null","expires_at":"gt."+_now(),
            "select":"profile_id"},"return=representation")
        if isinstance(rows,list) and len(rows)==1:
            profile=_valid_uuid(rows[0]["profile_id"])
            # Never move a previously linked token to a different learner.
            old=_request(secrets,"GET","/rest/v1/chokomana_device_links",params={
                "device_hash":"eq."+device_hash,"select":"profile_id,source_app","limit":"1"})
            if old and (old[0]["source_app"]!="kana" or old[0]["profile_id"]!=profile):
                raise SyncError("device_already_linked")
            _request(secrets,"POST","/rest/v1/chokomana_device_links",
                {"device_hash":device_hash,"source_app":"kana","profile_id":profile},
                {"on_conflict":"device_hash"},"resolution=ignore-duplicates,return=minimal")
    linked=_request(secrets,"GET","/rest/v1/chokomana_device_links",params={
        "device_hash":"eq."+device_hash,"source_app":"eq.kana","select":"profile_id","limit":"1"})
    return _valid_uuid(linked[0]["profile_id"]) if isinstance(linked,list) and linked else ""


def verify_plan_ticket(secrets,ticket:str | None,profile:str) -> dict | None:
    if not ticket or not profile:return None
    rows=_request(secrets,"GET","/rest/v1/chokomana_launch_tickets",params={
        "ticket_hash":"eq."+hashlib.sha256(ticket.encode()).hexdigest(),
        "profile_id":"eq."+profile,"source_app":"eq.kana","expires_at":"gt."+_now(),
        "select":"plan_item_id,menu_id,content_version,launch_mode,used_at","limit":"1"})
    if not isinstance(rows,list) or len(rows)!=1:return None
    p=rows[0]
    if not p.get("used_at") or p.get("menu_id") not in MENU_BY_ID or p.get("content_version")!=CONTENT_VERSION or p.get("launch_mode")!="challenge":return None
    if p.get("plan_item_id"):
        try:_valid_uuid(p["plan_item_id"])
        except SyncError:return None
    return {"menu_id":p["menu_id"],"plan_item_id":p.get("plan_item_id"),"launch_mode":"challenge"}


def validate_events(events:list,profile:str) -> list:
    if not isinstance(events,list) or len(events)>50:raise SyncError("invalid_batch")
    valid_types={"session_started","item_presented","response_submitted","support_used","learning_activity",
        "time_interval_closed","session_paused","session_resumed","session_ended","record_corrected"}
    clean=[]
    for value in events:
        if not isinstance(value,dict) or value.get("schema_version")!=1 or value.get("source_app")!=SOURCE_APP or value.get("profile_id")!=profile:raise SyncError("profile_mismatch")
        e={k:value.get(k) for k in ("schema_version","event_id","profile_id","source_app","app_version","content_version","attempt_id","occurrence_id","question_id","segment_id","sequence","occurred_at","event_type","payload")}
        for k in ("event_id","attempt_id","segment_id"):e[k]=_valid_uuid(e[k])
        if e["occurrence_id"] is not None:e["occurrence_id"]=_valid_uuid(e["occurrence_id"])
        if e["content_version"]!=CONTENT_VERSION or e["app_version"]!=APP_VERSION:raise SyncError("invalid_version")
        if e["event_type"] not in valid_types or not isinstance(e["sequence"],int) or not 0<e["sequence"]<2**31:raise SyncError("invalid_event")
        if not isinstance(e["occurred_at"],str) or not e["occurred_at"].endswith("Z"):
            raise SyncError("invalid_date")
        try:dt.datetime.fromisoformat(e["occurred_at"].replace("Z","+00:00"))
        except Exception as exc:raise SyncError("invalid_date") from exc
        p=e["payload"]
        if not isinstance(p,dict):raise SyncError("invalid_payload")
        if e["event_type"]=="session_started":
            if p.get("menu_id") not in MENU_BY_ID:raise SyncError("invalid_menu")
        if e["event_type"]=="item_presented":
            if e["question_id"] not in QUESTION_BANK or e["occurrence_id"] is None or p.get("presentation_kind") not in ("new","scheduled_repeat","retry"):raise SyncError("invalid_question")
            q=QUESTION_BANK[e["question_id"]]
            if p.get("question_snapshot",{}).get("prompt")!=q["prompt"] or p.get("question_snapshot",{}).get("answer")!=q["answer"]:raise SyncError("invalid_question")
        if e["event_type"]=="response_submitted":
            q=QUESTION_BANK.get(e["question_id"])
            if not q or p.get("response_status")!="answered" or p.get("assessment_method")!="automatic" or p.get("input_method")!="choice":raise SyncError("invalid_response")
            if p.get("response_value") not in q["pool"] or p.get("is_correct") is not (p["response_value"]==q["answer"]):raise SyncError("correctness_mismatch")
            if not isinstance(p.get("response_index"),int) or p["response_index"]<1 or not p.get("response_id"):raise SyncError("invalid_response")
            _valid_uuid(p["response_id"])
            if p.get("timing_quality") not in ("complete","partial","unknown","estimated"):raise SyncError("invalid_quality")
            if p.get("speech_status")!="not_used":raise SyncError("invalid_response")
        if e["event_type"]=="support_used" and p.get("support_type") not in ("hint","explanation","answer","replay","worked_example","parent"):raise SyncError("invalid_support")
        if e["event_type"]=="time_interval_closed":
            if p.get("phase") not in ("prompt","thinking","answering","feedback","explanation","exploration","reading_aloud","other"):raise SyncError("invalid_phase")
            if p.get("timing_quality") not in ("complete","partial","estimated","unknown"):raise SyncError("invalid_quality")
            for k in ("elapsed_ms","active_ms"):
                if p.get(k) is not None and (type(p[k]) is not int or not 0<=p[k]<=31536000000):raise SyncError("invalid_time")
            if p.get("active_ms") is not None and p["active_ms"]>p.get("elapsed_ms",-1):raise SyncError("invalid_time")
            if not isinstance(p.get("excluded_ms"),dict) or not all(isinstance(p["excluded_ms"].get(k),int) for k in ("hidden","explicit_pause","blocking_wait")):raise SyncError("invalid_time")
            for k in ("start_at","end_at"):
                if not isinstance(p.get(k),str) or not p[k].endswith("Z"):raise SyncError("invalid_time")
        if e["event_type"]=="session_ended" and p.get("status") not in ("completed","abandoned","interrupted","timed_out"):raise SyncError("invalid_status")
        if not isinstance(e.get("question_id"),(str,type(None))) or (e["question_id"] and e["question_id"] not in QUESTION_BANK):raise SyncError("invalid_question")
        if len(json.dumps(e,ensure_ascii=False).encode())>16000:raise SyncError("event_too_large")
        e["event_hash"]=hashlib.sha256(json.dumps(e,ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        clean.append(e)
    return clean


def verified_stamp(secrets,profile:str,attempt_id:str) -> dict:
    # Check persisted events, not client-supplied result. Return only one per attempt.
    rows=_request(secrets,"GET","/rest/v1/chokomana_learning_events",params={
        "profile_id":"eq."+profile,"source_app":"eq.kana","attempt_id":"eq."+attempt_id,
        "select":"event_type,occurrence_id,question_id,payload,sequence","limit":"5000"})
    if not isinstance(rows,list):raise SyncError("result_unavailable")
    starts=[x for x in rows if x["event_type"]=="session_started"]
    ends=[x for x in rows if x["event_type"]=="session_ended" and x["payload"].get("status")=="completed"]
    if len(starts)!=1 or len(ends)!=1:raise SyncError("not_completed")
    menu=MENU_BY_ID.get(starts[0]["payload"].get("menu_id"))
    if menu is None:raise SyncError("unknown_menu")
    presented={x["occurrence_id"]:x for x in rows if x["event_type"]=="item_presented"}
    responses={}
    for e in rows:
        if e["event_type"]=="response_submitted":
            oid=e["occurrence_id"]
            if oid in responses and e["payload"].get("response_index",99)>=responses[oid]["payload"].get("response_index",99):continue
            responses[oid]=e
    if len(presented)!=menu["question_count_default"] or len(responses)!=len(presented):raise SyncError("incomplete_attempt")
    correct=0
    for oid,p in presented.items():
        q=QUESTION_BANK.get(p["question_id"])
        r=responses.get(oid)
        if q is None or q["menu_id"]!=menu["menu_id"] or r is None or r["question_id"]!=p["question_id"]:raise SyncError("invalid_question")
        correct+=int(r["payload"].get("response_value")==q["answer"])
    total=len(presented)
    ratio=correct/total
    grade="gold" if ratio==1 else "silver" if ratio>=0.8 else "bronze" if ratio>=0.6 else "white"
    end=ends[0]["payload"]
    finished=end.get("finished_at") or _now()
    try:finished_jst=dt.datetime.fromisoformat(finished.replace("Z","+00:00")).astimezone(dt.timezone(dt.timedelta(hours=9))).date().isoformat()
    except Exception:raise SyncError("invalid_date")
    return {"stamp_id":str(uuid.uuid5(uuid.NAMESPACE_URL,f"kana:{profile}:{attempt_id}")),
        "profile_id":profile,"source_app":"kana","attempt_id":attempt_id,
        "category_key":menu["category_id"]+"_"+menu["subject"],"category_label":menu["label"],
        "stamp_char":"字" if menu["subject"]=="letters" else "語", "reading":"ジ" if menu["subject"]=="letters" else "ゴ",
        "difficulty":menu["difficulty_label"],"correct_count":correct,"total_count":total,
        "grade":grade,"finished_at":finished,"study_date":finished_jst}


def publish_catalog(secrets,base_url="",question_ids=None):
    rows=catalog_rows(base_url)
    if question_ids:rows+=question_catalog_rows(question_ids)
    # HTTP upserts are idempotent; existing content versions are immutable.
    for i in range(0,len(rows),80):
        _request(secrets,"POST","/rest/v1/chokomana_learning_catalog",rows[i:i+80],
            {"on_conflict":"source_app,entry_type,entry_id,content_version"},
            "resolution=ignore-duplicates,return=minimal")


def publish_question_catalog(secrets,ids):
    # Unlike a full menu upload, this is at most one small batch of questions.
    rows=question_catalog_rows(ids)
    if rows:_request(secrets,"POST","/rest/v1/chokomana_learning_catalog",rows,
        {"on_conflict":"source_app,entry_type,entry_id,content_version"},
        "resolution=ignore-duplicates,return=minimal")

def process_message(st,message:dict) -> dict:
    deliver=str(message.get("delivery_id",""))
    result={"delivery_id":deliver,"ok":False,"code":"invalid_request"}
    try:
        _valid_uuid(deliver)
        kind=message.get("kind")
        token=message.get("device_token","")
        if kind=="normalize":
            data=normalize_spoken_word(message.get("text",""),message.get("script",""),message.get("word_mode","auto"))
            result.update(ok=True,code="ok",normalized=data)
            return result
        if kind=="pdf":
            t0=time.perf_counter()
            pdf=generate_worksheet_pdf(message.get("text",""),message.get("script",""),message.get("orientation",""))
            result.update(ok=True,code="ok",bytes=len(pdf),pdf_base64=base64.b64encode(pdf).decode("ascii"),pdf_ms=round((time.perf_counter()-t0)*1000,1))
            return result
        # No network on the first screen. Identity resolves via an explicit, asynchronous component message.
        secrets=st.secrets
        ticket=st.session_state.get("_kana_ticket") if kind=="identity" else None
        profile=resolve_identity(secrets,token,ticket)
        st.session_state["_kana_profile"]=profile
        if ticket and profile:
            st.session_state["_kana_verified_assignment"]=verify_plan_ticket(secrets,ticket,profile)
            st.session_state.pop("_kana_ticket",None)
            try:del st.query_params["cm_launch"]
            except Exception:pass
        if kind=="identity":
            result.update(ok=True,code="linked" if profile else "unlinked",profile_id=profile,
                          assignment=st.session_state.get("_kana_verified_assignment"))
            return result
        if not profile:raise SyncError("not_linked")
        if kind=="sync":
            events=validate_events(message.get("events",[]),profile)
            if not events:raise SyncError("empty_batch")
            if not st.session_state.get("_kana_catalog_published"):
                publish_catalog(secrets,str(secrets.get("KANA_APP_URL","")))
                st.session_state["_kana_catalog_published"]=True
            qids=[e["question_id"] for e in events if e["event_type"]=="item_presented"]
            if qids:publish_question_catalog(secrets,qids)
            ack=_request(secrets,"POST","/rest/v1/rpc/chokomana_append_kana_events_v1",
                {"p_device_hash":_device_hash(token),"p_events":events})
            result.update(ok=True,code="saved",acked=ack.get("acked",[]) if isinstance(ack,dict) else [])
            return result
        if kind=="stamp":
            aid=_valid_uuid(message.get("attempt_id"))
            data=verified_stamp(secrets,profile,aid)
            _request(secrets,"POST","/rest/v1/chokomana_stamps",data,
                {"on_conflict":"profile_id,source_app,attempt_id"},"resolution=ignore-duplicates,return=minimal")
            result.update(ok=True,code="stamp_saved",stamp={k:data[k] for k in ("attempt_id","grade","correct_count","total_count","stamp_char","reading")})
            return result
        raise SyncError("invalid_kind")
    except SyncError as exc:result["code"]=str(exc)
    except (ValueError,TypeError) as exc:result["code"]="invalid_input"
    except HTTPError as exc:result["code"]="database_unavailable" if exc.code>=500 else "database_rejected"
    except (URLError,TimeoutError):result["code"]="network_unavailable"
    except Exception:result["code"]="operation_failed"
    return result


# UI HTML appended below; the entire JS quiz runs client-side without Streamlit reruns.
HTML_TEMPLATE = r'''<!doctype html>
<html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<style>
:root{--ink:#292b2a;--ink2:#62665f;--paper:#f7f5ef;--white:#fffefa;--border:#e2d9c9;--blue:#315f86;--blue2:#eaf2f8;--gold:#e8c269;--warn:#a74b37}
*{box-sizing:border-box}html{scroll-behavior:auto}body{margin:0;padding:0;color:var(--ink);background:var(--paper);font-family:system-ui,-apple-system,'Hiragino Sans','Yu Gothic',sans-serif}
button,input,textarea,select{font:inherit}button,a.btn{border:1px solid #d9d0c1;background:#f4f0e8;color:var(--ink);border-radius:13px;padding:10px 14px;min-height:48px;cursor:pointer;font-weight:850;touch-action:manipulation;text-decoration:none;display:inline-flex;justify-content:center;align-items:center;line-height:1.3;gap:5px}
button.primary,a.primary{background:var(--blue);border-color:var(--blue);color:white}button:focus-visible,a:focus-visible,input:focus-visible,textarea:focus-visible,select:focus-visible{outline:3px solid #6098b8;outline-offset:3px}button:disabled{opacity:.55;cursor:default}
.app{width:min(920px,100%);margin:0 auto;padding:12px max(13px,env(safe-area-inset-right)) 30px}
.topline{display:flex;align-items:center;justify-content:space-between;gap:8px;flex-wrap:wrap;min-height:44px;margin-bottom:8px}
.mini{font-size:12px;color:var(--ink2)}.links{display:flex;gap:7px;flex-wrap:wrap}.links a{font-size:13px;min-height:41px;padding:8px 12px}
.site-title{font-weight:1000;font-size:clamp(25px,5vw,35px);letter-spacing:.015em;margin:5px 0 8px}.intro{color:#625b50;margin:0 0 17px;font-size:14px}
.tabs{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:7px;margin:0 0 15px}.tabs button{font-size:14px;background:#eee8de;border-color:#e0d6c8;padding:10px 4px}.tabs button.active{background:#315f86;color:#fff;border-color:#315f86}
.pane{display:none}.pane.active{display:block}
.group-heading{font-size:18px;font-weight:950;margin:18px 1px 9px}.group-heading:first-child{margin-top:3px}.menu-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
.menu{width:100%;min-height:96px;display:flex;align-items:flex-start;flex-direction:column;gap:6px;text-align:left;padding:14px;border-radius:17px;background:linear-gradient(130deg,#fffefa,#f5ecda);border:1px solid #d9c9ae;box-shadow:0 3px 12px #594f3810}
.menu.katakana{background:linear-gradient(130deg,#ffffff,#eaf2f9);border-color:#c1d3e3}
.menu .menu-title{font-size:clamp(16px,3.6vw,20px);font-weight:950;line-height:1.3}.menu .menu-meta{color:#746a59;font-size:12px;font-weight:750}
.menu .menu-go{font-size:12px;color:#315f86;font-weight:850;align-self:flex-end}
.view{display:none}.view.active{display:block}.inview-head{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:14px}.inview-head a,.inview-head button{min-height:42px;padding:9px 12px;font-size:13px}
.game-card,.paper-card,.settings-card,.result-card{background:var(--white);border:1px solid var(--border);border-radius:20px;padding:clamp(14px,3vw,23px);box-shadow:0 4px 16px #4c403314}
.overline{color:#797165;font-weight:800;font-size:13px}.progress{height:7px;background:#eee8df;border-radius:12px;overflow:hidden;margin:9px 0 12px}.progress span{display:block;background:#5984a5;height:100%;transition:width .15s}
.question-caption{font-size:clamp(18px,4.5vw,23px);font-weight:900;margin:12px 0 1px;text-align:center}.question{font-size:clamp(50px,11vw,83px);font-weight:900;text-align:center;line-height:1.32;margin:8px auto 15px;overflow-wrap:anywhere;letter-spacing:.02em}
.choices{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.choice{background:#fff;border:2px solid #d2d8da;border-radius:15px;min-height:92px;padding:10px 6px;font-size:clamp(26px,6vw,40px);font-weight:950;overflow-wrap:anywhere}
.choice.correct{background:#eef7ef;border-color:#65a079;color:#24573a}.choice.wrong{background:#fff1ec;border-color:#c48f78;color:#7f3325}.choice:disabled{opacity:1}
.feedback{min-height:56px;font-size:20px;font-weight:950;text-align:center;margin:18px 0 8px;color:#2e6949}.feedback.bad{color:#a44431}.game-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px;margin-top:9px}.game-actions button{min-height:51px}.under-actions{display:flex;gap:8px;justify-content:space-between;flex-wrap:wrap;margin-top:13px}.under-actions button{font-size:12px;min-height:40px}
.hint-status{font-size:13px;color:#5c6671;min-height:20px;text-align:center}
.medal{width:106px;height:106px;margin:8px auto 15px;border-radius:50%;display:flex;flex-direction:column;align-items:center;justify-content:center;border:5px double #aab7c3;box-shadow:inset 0 5px 12px #ffffffaf,0 5px 16px #252b3925}
.medal.gold{background:radial-gradient(circle at 25% 20%,#fff9d0,#f2d064 52%,#c38d1d);border-color:#bd8c26;color:#6a4a16}.medal.silver{background:radial-gradient(circle at 25% 20%,#fff,#d8e0e8 53%,#9aa8b8);border-color:#9aa9b8;color:#3d5266}.medal.bronze{background:radial-gradient(circle at 25% 20%,#fff1dc,#d99c6e 53%,#a6653d);border-color:#ad7044;color:#60391e}.medal.white{background:radial-gradient(circle at 25% 20%,#fff,#f8f9fb 54%,#d7dde2);border-color:#b9c2cb;color:#4c5b6a}.medal small{font-size:10px;font-weight:900}.medal strong{font-size:36px;line-height:1.2}.medal em{font-size:11px;font-weight:850;font-style:normal}
.result-title{text-align:center;font-size:27px;font-weight:950;margin:8px}.result-score{text-align:center;font-size:20px;margin:7px}.result-status{text-align:center;font-size:13px;color:#687071;min-height:23px}.result-actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px;margin-top:16px}
.paper-header{font-weight:950;font-size:20px;margin:0 0 10px}.input-label{font-size:13px;font-weight:800;color:#5e615e;margin:14px 0 6px}.seg{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}.seg.three{grid-template-columns:repeat(3,minmax(0,1fr))}.seg button{background:#fff;font-size:14px;min-height:45px;padding:7px 4px}.seg button.selected{background:#e6f0f7;border:2px solid #477aa0;color:#194b71}
.input-word{width:100%;border:1px solid #b8bdc1;border-radius:13px;padding:14px;font-size:clamp(21px,5vw,27px);background:#fff;min-height:58px}
.inline-actions{display:flex;align-items:center;gap:9px;flex-wrap:wrap;margin:10px 0}.muted{color:#71746f;font-size:12px;line-height:1.5}.notice{color:#6e6b61;font-size:13px;min-height:18px;margin-top:9px}
.preview{border:1px solid #c8c8c8;background:#fff;aspect-ratio:210/297;width:min(330px,100%);padding:15px;margin:12px auto;overflow:hidden}.preview.horizontal{display:flex;flex-direction:column;gap:7px;align-items:flex-start;justify-content:flex-start}.preview.vertical{display:flex;flex-direction:row-reverse;gap:5px;align-items:flex-start;justify-content:flex-start}
.trace-group{display:flex;gap:2px}.preview.vertical .trace-group{flex-direction:column}.cells{display:flex;gap:0}.preview.vertical .cells{flex-direction:column}.cell{width:clamp(23px,6.7vw,36px);height:clamp(23px,6.7vw,36px);border:.7px solid #bdbdbd;margin-right:-.7px;margin-bottom:-.7px;position:relative;display:grid;place-items:center;color:#b7b7b7;font-weight:650;font-size:clamp(17px,5vw,25px)}.cell:before,.cell:after{content:"";position:absolute;pointer-events:none;opacity:.6}.cell:before{top:50%;left:0;right:0;border-top:1px dashed #e5e5e5}.cell:after{left:50%;top:0;bottom:0;border-left:1px dashed #e5e5e5}.cell span{position:relative;z-index:1}
.preview.horizontal .trace-group{flex-direction:column;gap:3px}.preview.vertical .trace-group{flex-direction:row-reverse;gap:3px}
.download-row{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:10px}
.summary-metrics{display:grid;grid-template-columns:repeat(3,1fr);gap:7px;margin:8px 0 12px}.metric{background:#f6f1e8;border-radius:12px;text-align:center;padding:11px 5px;font-size:12px}.metric strong{display:block;font-size:21px;font-variant-numeric:tabular-nums;color:#36566c}
.settings-card details{border-top:1px solid #e5ded3;padding-top:10px;margin-top:11px}.settings-card summary{cursor:pointer;font-size:14px;font-weight:850}.settings-card p{line-height:1.55}
pre{font-size:11px;overflow:auto;white-space:pre-wrap;max-height:175px;background:#f4f5f4;border-radius:9px;padding:9px}
.version{text-align:center;font-size:10px;color:#8b8983;padding:20px 0 5px}
@media(max-width:450px){.app{padding:9px 10px 20px}.menu{padding:11px;min-height:91px}.choice{min-height:77px}.game-actions{gap:7px}.tabs button{font-size:13px}.preview{padding:12px}.cell{width:26px;height:26px;font-size:18px}}
</style></head><body><main id="app" class="app">
<div id="normal-view" class="view active">
<div class="topline"><div class="mini">ちょこまな 学習アプリ</div><div class="links"><a class="btn" id="hubTop" target="_blank" rel="noopener noreferrer">← ちょこまなへ</a></div></div>
<div class="site-title">ひらがな・カタカナ</div><div class="tabs"><button id="tab-challenge" class="active" data-tab="challenge">チャレンジ</button><button id="tab-paper" data-tab="paper">こうひつ</button><button id="tab-settings" data-tab="settings">設定</button></div>
<section id="pane-challenge" class="pane active"><div class="intro">やりたい もんだいを えらぼう。</div><div id="menuContainer"></div><p class="muted" id="connectionState">ちょこまな：未連携（この端末には記録できます）</p></section>
<section id="pane-paper" class="pane"><div class="paper-card">
<h2 class="paper-header">こうひつ プリント</h2>
<div class="input-label">もじ</div><div id="scriptSegment" class="seg"><button data-script="hiragana" class="selected">ひらがな</button><button data-script="katakana">カタカナ</button></div>
<div class="input-label">ことばを いってね</div><input id="wordText" class="input-word" type="text" placeholder="ここに ことばが はいるよ" maxlength="60" autocomplete="off" spellcheck="false">
<div class="inline-actions"><button id="voiceBtn" class="primary">マイクで はなす</button><span id="voiceState" class="muted">マイクがつかえないときは もじをいれてね。</span></div>
<div class="input-label">ことばの あつかい</div><div class="seg three" id="wordMode"><button data-word-mode="auto" class="selected">じどう</button><button data-word-mode="usual">ふつう</button><button data-word-mode="playful">おもしろ</button></div>
<p class="muted">おもしろい ことばは そのままに。じどうで はんだんできない ことばは かってに なおしません。</p>
<div class="input-label">かきかた</div><div class="seg" id="directionSegment"><button data-orientation="horizontal" class="selected">よこがき</button><button data-orientation="vertical">たてがき</button></div>
<div class="inline-actions"><button id="previewBtn" class="primary">このことばで いい？</button><span id="paperInfo" class="notice"></span></div>
<div id="previewSection" hidden><div class="input-label">いんさつ まえの プレビュー</div><div id="previewPaper" class="preview horizontal"></div>
<div class="download-row"><button id="downloadPdf" class="primary">PDFを つくる</button><button id="editPdf">ことばを なおす</button></div><p id="pdfStatus" class="muted"></p></div>
</div></section>
<section id="pane-settings" class="pane"><div class="settings-card"><div class="paper-header">設定・動作ログ</div>
<div class="input-label">音声</div><div class="seg" id="soundSwitch"><button data-sound="on" class="selected">音声 ON</button><button data-sound="off">音声 OFF</button></div>
<div class="input-label">音量 <output id="volumeOutput">100%</output></div><input id="volumeSlider" type="range" min="0" max="100" value="100" style="width:100%">
<div class="input-label">ちょこまな連携</div><p class="muted" id="settingsConnection">未連携</p>
<div class="summary-metrics"><div class="metric"><strong id="metricSessions">0</strong>チャレンジ</div><div class="metric"><strong id="metricPending">0</strong>未送信イベント</div><div class="metric"><strong id="metricErrors">0</strong>エラー</div></div>
<div class="inline-actions"><button id="syncNow">ちょこまなへ送信</button><button id="exportLearning">学習ログを出力</button></div>
<details><summary>起動・操作速度を確認する</summary><p class="muted">ボタン押下→最初の問題が表示されるまでを計測。Streamlit起動前のホーム画面タップ時刻は別アプリからは取得できません。</p>
<pre id="timingSummary">ログなし</pre><div class="inline-actions"><button id="exportDiagnosticsJson">動作ログ JSON</button><button id="exportDiagnosticsCsv">動作ログ CSV</button><button id="clearDiagnostics">動作ログを消す</button></div></details>
<details><summary>記録の扱い</summary><p class="muted">学習ログは端末に残り、検証済みの本人IDに限ってSupabaseへ送信します。未連携の履歴を別人に自動統合しません。正誤は画面の四択のみ。紙への筆記は自動採点しません。未送信の学習ログは削除しません。</p></details>
</div></section></div>
<div id="game-view" class="view"><div class="inview-head"><button class="backHome">← TOPへ</button><a class="btn hubInGame" target="_blank" rel="noopener noreferrer">ちょこまなへ</a><button id="pauseButton" style="margin-left:auto">きゅうけい</button></div>
<div class="game-card"><div id="gameStep" class="overline"></div><div class="progress"><span id="progressBar" style="width:0%"></span></div>
<div id="questionCaption" class="question-caption">おなじ よみは どれ？</div><div id="questionMain" class="question"></div><div id="choiceGrid" class="choices"></div>
<div id="answerFeedback" class="feedback" aria-live="polite"></div><div id="hintState" class="hint-status"></div>
<div class="game-actions"><button id="hintButton">ヒント（よみあげ）</button><button id="nextButton" class="primary" disabled>つぎへ</button></div>
<div class="under-actions"><button id="retryButton" hidden>もういちど えらぶ</button><button id="abandonButton">おわる（とちゅう）</button></div></div></div>
<div id="result-view" class="view"><div class="inview-head"><button class="backHome">← TOPへ</button><a class="btn hubInGame" target="_blank" rel="noopener noreferrer">ちょこまなへ</a></div>
<div class="result-card"><div class="result-title">おつかれさま！</div><div class="medal white" id="resultMedal"><small id="resultGrade"></small><strong id="resultChar">字</strong><em id="resultReading">ジ</em></div>
<div id="resultScore" class="result-score"></div><div id="resultStatus" class="result-status"></div>
<div class="result-actions"><button id="againButton" class="primary">もういっかい</button><button class="backHome">TOPへ</button><a class="btn" id="hubResult" target="_blank" rel="noopener noreferrer">ちょこまなへ</a></div></div></div>
<div class="version">ひらがな・カタカナ v{{APP_VERSION}}</div></main>
<script>
/* Standard Streamlit component postMessage protocol, with no external JS CDN. */
if(!window.Streamlit && window.parent !== window){
 const callbacks=[];
 window.Streamlit={
  RENDER_EVENT:'streamlit:render',
  events:{addEventListener:(name,cb)=>{if(name==='streamlit:render')callbacks.push(cb)}},
  setComponentReady:()=>window.parent.postMessage({isStreamlitMessage:true,type:'streamlit:componentReady',apiVersion:1},'*'),
  setComponentValue:value=>window.parent.postMessage({isStreamlitMessage:true,type:'streamlit:setComponentValue',value},'*'),
  setFrameHeight:height=>window.parent.postMessage({isStreamlitMessage:true,type:'streamlit:setFrameHeight',height},'*'),
 };
 window.addEventListener('message',e=>{if(e.source!==window.parent || e.data?.type!=='streamlit:render')return;
  for(const cb of callbacks)cb({detail:{args:e.data.args||{}}});});
}
window.__kanaBootAt=performance.now();
const MENU_DATA={{MENU_JSON}},BANK={{BANK_JSON}},ALL_MENUS=Object.values(MENU_DATA);
const APP_VERSION={{VERSION_JSON}},CONTENT_VERSION={{CONTENT_JSON}},HOME_URL={{HUB_URL_JSON}};
const $=q=>document.querySelector(q), $$=q=>Array.from(document.querySelectorAll(q));
const visible=(el,v)=>{el.hidden=!v;};
const isUUID=()=> (crypto.randomUUID?crypto.randomUUID(): 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g,c=>{const n=crypto.getRandomValues(new Uint8Array(1))[0]&15;return (c==='x'?n:(n&3|8)).toString(16)}));
const nowISO=()=>new Date().toISOString();const performanceLogKey='chokomana_kana_diagnostics_v1';
const safeGet=(key,fallback)=>{try{const v=JSON.parse(localStorage.getItem(key));return v??fallback}catch(e){return fallback}};
const safePut=(key,v)=>{try{localStorage.setItem(key,JSON.stringify(v));return true}catch(e){$('#connectionState').textContent='端末に記録を保存できません。ブラウザの空き容量を確認してください。';return false}};
const EVENTS_KEY='chokomana_kana_events_v1',STATE_KEY='chokomana_kana_state_v1',DEVICE_KEY='chokomana_device_identity_v1_kana', LEASE_KEY='chokomana_kana_lease_v1';
const HIST_KEY='chokomana_kana_completed_v1', SETTINGS_KEY='chokomana_kana_settings_v1';
const sessionId=isUUID(), localId=safeGet('chokomana_kana_local_learner_v1',null)||isUUID();safePut('chokomana_kana_local_learner_v1',localId);
let deviceToken='';try{deviceToken=localStorage.getItem(DEVICE_KEY)||'';if(!/^[a-f0-9]{64}$/.test(deviceToken)){const a=crypto.getRandomValues(new Uint8Array(32));deviceToken=Array.from(a,x=>x.toString(16).padStart(2,'0')).join('');localStorage.setItem(DEVICE_KEY,deviceToken)}}catch(e){}
let currentProfile='', identityStatus='unlinked',appSettings=safeGet(SETTINGS_KEY,{sound:true,volume:100}),
 eventDB=safeGet(EVENTS_KEY,{pending:[],acked:[],local:[]}),history=safeGet(HIST_KEY,[]),game=safeGet(STATE_KEY,null);
let diagnostics=safeGet(performanceLogKey,[]),lastResultDelivery='',eventClock=null,leaseOwned=false,requestBusy=null,requestQueue=[],currentPane='challenge',currentView='normal', recognition=null;
let script='hiragana',orientation='horizontal',wordMode='auto',lastWord='',pdfPending=null,deliveredStamps=safeGet('chokomana_kana_stamp_ack_v1',{});
let syncResultTimer=null; const launchedPlanIds=new Set();
const diag=(type,detail={})=>{const item={at:nowISO(),type,...detail};diagnostics.push(item);if(diagnostics.length>1800)diagnostics=diagnostics.slice(-1800);safePut(performanceLogKey,diagnostics)};
const diagTiming=(type,start,extra={})=>diag(type,{duration_ms:Math.round((performance.now()-start)*100)/100,...extra});
diag('startup_js',{iframe_js_ready_ms:+performance.now().toFixed(1),time_origin_ms:performance.timeOrigin||null,document_ready_state:document.readyState,
 iframe_navigation:performance.getEntriesByType?.('navigation')?.[0] ? {response_end_ms:performance.getEntriesByType('navigation')[0].responseEnd,dom_interactive_ms:performance.getEntriesByType('navigation')[0].domInteractive}:null});
function exportBlob(name,content,mime){const file=new Blob([content],{type:mime});const url=URL.createObjectURL(file);const a=document.createElement('a');a.href=url;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(url),2000)}
function setNavLinks(){for(const el of [$('#hubTop'),...$$('.hubInGame'),$('#hubResult')])el.href=HOME_URL;}
function setView(name){currentView=name;for(const el of $$('.view'))el.classList.toggle('active',el.id===name+'-view');setHeight();scrollTo({top:0,behavior:'instant'});}
function setTab(name){currentPane=name;$$('[data-tab]').forEach(b=>b.classList.toggle('active',b.dataset.tab===name));for(const el of $$('.pane'))el.classList.toggle('active',el.id==='pane-'+name);setHeight();}
let pendingHeight=false,lastFrameHeight=0;
function setHeight(){if(pendingHeight)return;pendingHeight=true;requestAnimationFrame(()=>{pendingHeight=false;
 const root=$('#app'),bounds=root.getBoundingClientRect(),height=Math.max(1,Math.ceil(bounds.bottom+(window.scrollY||0)+8));
 if(height===lastFrameHeight)return;lastFrameHeight=height;
 try{window.Streamlit?.setFrameHeight?.(height)}catch(e){};
});}
function renderMenus(){const root=$('#menuContainer');root.textContent='';for(const key of ['hiragana','katakana']){
 const h=document.createElement('div');h.className='group-heading';h.textContent=key==='hiragana'?'ひらがな':'カタカナ';root.appendChild(h);
 const grid=document.createElement('div');grid.className='menu-grid';for(const m of ALL_MENUS.filter(m=>m.script===key)){
  const b=document.createElement('button');b.className='menu '+key;b.type='button';b.dataset.menu=m.menu_id;
  const name=document.createElement('span');name.className='menu-title';name.textContent=(m.subject==='letters'?'もじ':'ことば')+' '+(m.level===1?'初級':'中級');
  const meta=document.createElement('span');meta.className='menu-meta';meta.textContent=m.count+'もん ・ '+(m.subject==='letters'?'もじを えらぶ':'ことばを えらぶ');
  const go=document.createElement('span');go.className='menu-go';go.textContent='はじめる';b.append(name,meta,go);b.addEventListener('pointerdown',()=>{window.__kanaLastMenuPointerAt=performance.now()},{passive:true});b.addEventListener('click',()=>beginGame(m.menu_id));grid.appendChild(b);
 }root.appendChild(grid);
 }requestAnimationFrame(()=>requestAnimationFrame(()=>diag('home_ready',{iframe_js_to_paint_ms:+performance.now().toFixed(1),visible:!document.hidden})));
}
function choose(list,n){const a=list.slice();for(let i=a.length-1;i>0;i--){let k=Math.floor(Math.random()*(i+1));[a[i],a[k]]=[a[k],a[i]]}return a.slice(0,n)};
const qbankByMenu=id=>BANK.filter(q=>q.menu_id===id);
function prepareQuestions(menuId){const m=MENU_DATA[menuId],pool=qbankByMenu(menuId),selected=choose(pool,m.count);
 return selected.map(q=>{const options=choose(Array.from(new Set([q.answer,...choose(pool.map(x=>x.answer).filter(x=>x!==q.answer),Math.max(3,pool.length-1))])).slice(0,4),4);
 return {question_id:q.question_id,prompt:q.prompt,answer:q.answer,options,occurrence_id:isUUID(),responses:[],hint:0,answerShown:false};});}
function storeGame(){safePut(STATE_KEY,game)};
function lease(){const stamp=Date.now();let other=null;try{other=JSON.parse(localStorage.getItem(LEASE_KEY)||'null')}catch(e){};
 if(other?.owner!==sessionId&&other?.expiry>stamp){leaseOwned=false;return false;}
 try{localStorage.setItem(LEASE_KEY,JSON.stringify({owner:sessionId,expiry:stamp+9000,attempt_id:game?.attempt_id||''}));leaseOwned=true;return true}catch(e){leaseOwned=false;return false;}}
function releaseLease(){try{const lease=JSON.parse(localStorage.getItem(LEASE_KEY)||'null');if(lease?.owner===sessionId)localStorage.removeItem(LEASE_KEY)}catch(e){}leaseOwned=false}
function logEvent(type,payload={},question=null){if(!game)return null;
 const e={schema_version:1,event_id:isUUID(),profile_id:game.profile_id||null,source_app:'kana',app_version:APP_VERSION,content_version:CONTENT_VERSION,
 attempt_id:game.attempt_id,occurrence_id:question?.occurrence_id||null,question_id:question?.question_id||null,
 segment_id:game.segment_id,sequence:++game.sequence,occurred_at:nowISO(),event_type:type,payload};
 if(e.profile_id){eventDB.pending.push(e)}else eventDB.local.push(e);
 if(!safePut(EVENTS_KEY,eventDB))diag('storage_error',{category:'learning_outbox'});storeGame();return e;
}
function currentQ(){return game?.questions?.[game.index]||null}
function closeClock(reason){if(!eventClock||!game)return;const {startPerf,startWall,phase}=eventClock;eventClock=null;const endPerf=performance.now(),endWall=Date.now();
 const ms=Math.max(0,Math.round(endPerf-startPerf)),wallMs=endWall-startWall;const clockOK=Math.abs(ms-wallMs)<1500 && ms<=15000 && wallMs>=0;
 if(ms<1)return;
 const q=currentQ();
 if(q){if(clockOK)q.active_ms=(q.active_ms||0)+wallMs;else q.time_unknown=true;}
 logEvent('time_interval_closed',{
  start_at:new Date(startWall).toISOString(),end_at:new Date(endWall).toISOString(),elapsed_ms:Math.max(ms,wallMs,0),
  monotonic_elapsed_ms:ms,active_ms:clockOK?wallMs:null,phase:phase||'answering',
  excluded_ms:{hidden:0,explicit_pause:0,blocking_wait:0},
  timing_quality:clockOK?'complete':'unknown',end_reason:reason,
 },q);
}
function startClock(){if(!game||game.finished||game.paused||document.hidden||currentView!=='game'||!lease())return;
 eventClock={startPerf:performance.now(),startWall:Date.now(),phase:currentQ()?.responses?.length?'feedback':'answering'};
}
function checkpointClock(reason){if(!game||game.finished)return;closeClock(reason);if(!game.paused&&!document.hidden)startClock()}
function markGameAfterPaint(start,menuId){requestAnimationFrame(()=>requestAnimationFrame(()=>{
 const pointerAt=window.__kanaLastMenuPointerAt;
 const pointerDelta=Number.isFinite(pointerAt)&&pointerAt<=start? Math.round((performance.now()-pointerAt)*100)/100:null;
 diag('challenge_first_problem_painted',{menu_id:menuId,input_to_problem_ready_ms:Math.round((performance.now()-start)*100)/100,
 pointer_to_problem_ready_ms:pointerDelta,measurement:'click_and_pointerdown_to_two_animation_frames',external_app_tap_to_first_problem_ms:null});
 window.__kanaLastMenuPointerAt=null;
 }));}
function beginGame(menuId,origin='menu_button'){
 const m=MENU_DATA[menuId];if(!m)return;
 const t=performance.now();diag('challenge_start_clicked',{menu_id:menuId,origin});
 if(game&&!game.finished){endGame('interrupted',true)}
 game={attempt_id:isUUID(),menu_id:menuId,questions:prepareQuestions(menuId),index:0,started_at:nowISO(),finished_at:null,
 profile_id:currentProfile||null,local_learner_id:localId,segment_id:isUUID(),sequence:0,paused:false,finished:false,score:0,
 assignment_id:window.__kanaAssignment?.menu_id===menuId?window.__kanaAssignment.plan_item_id||null:null};storeGame();
 logEvent('session_started',{menu_id:menuId,mode:'challenge',difficulty_id:String(m.level),progression_group_id:m.group,
 question_count:m.count,settings_snapshot:{sound_enabled:Boolean(appSettings.sound),volume_percent:appSettings.volume},
 assignment_context:game.assignment_id?{plan_item_id:game.assignment_id}:null});
 setView('game');showQuestion();startClock();markGameAfterPaint(t,menuId);
}
function showQuestion(){const q=currentQ();if(!q)return;const m=MENU_DATA[game.menu_id];
 $('#gameStep').textContent=m.label+'　'+(game.index+1)+' / '+m.count;
 $('#progressBar').style.width=(game.index/m.count*100)+'%';
 $('#questionCaption').textContent=(m.script==='hiragana'?'ひらがな':'カタカナ')+'で おなじ '+(m.subject==='letters'?'もじ':'ことば')+'を えらぼう';
 $('#questionMain').textContent=q.prompt;
 $('#choiceGrid').textContent='';q.options.forEach(opt=>{const b=document.createElement('button');b.className='choice';b.type='button';b.textContent=opt;
 b.addEventListener('click',()=>answer(opt));$('#choiceGrid').appendChild(b);});
 $('#answerFeedback').textContent='';$('#answerFeedback').classList.remove('bad');$('#hintState').textContent='';
 $('#nextButton').disabled=true;visible($('#retryButton'),false);$('#hintButton').disabled=false;
 $('#pauseButton').textContent=game.paused?'さいかい':'きゅうけい';
 const already=q.responses.length>0;if(already)showFeedback(q.responses.at(-1).value===q.answer,q.responses.at(-1).value,q);
 if(!q.presented){q.presented=true;logEvent('item_presented',{position:game.index+1,presentation_kind:'new',skill_ids:m.skill_ids,question_family_id:m.group,
  question_snapshot:{prompt:q.prompt,answer:q.answer,choice_ids:q.options},assessment_method:'automatic',answer_exposed_before:false,
  item_settings:{script:m.script,subject:m.subject,level:m.level}} ,q);}
 storeGame();setHeight();
}
function answer(value){if(!game||game.paused||game.finished)return;const q=currentQ();if(!q||q.locked)return;
 const isFirst=q.responses.length===0,correct=value===q.answer;
 closeClock('response');
 q.responses.push({id:isUUID(),value,correct,at:nowISO()});
 logEvent('response_submitted',{response_id:q.responses.at(-1).id,response_index:q.responses.length,response_status:'answered',
 response_value:value,is_correct:correct,assessment_method:'automatic',input_method:'choice',speech_status:'not_used',
 answer_latency_ms:q.time_unknown?null:(q.active_ms??null),timing_quality:q.time_unknown?'unknown':(q.restored?'partial':'complete'),support_before_response:{hint_count:q.hint,explanation_seen:false,
 answer_seen:q.answerShown,worked_example_seen:false,parent_help:null},skill_ids:MENU_DATA[game.menu_id].skill_ids,
 response_is_first:isFirst},q);
 if(!correct){q.answerShown=true;logEvent('support_used',{support_type:'answer',reason:'automatic_feedback_after_error'},q)}
 q.locked=true;storeGame();showFeedback(correct,value,q);startClock();if(appSettings.sound)say(correct?'せいかい！':'ざんねん！ いっしょに おぼえよう');
}
function showFeedback(correct,value,q){$('#answerFeedback').textContent=correct?'せいかい！':'ざんねん！ せいかいは '+q.answer;
 $('#answerFeedback').classList.toggle('bad',!correct);$$('.choice').forEach(b=>{
  b.disabled=true;b.classList.toggle('correct',b.textContent===q.answer);b.classList.toggle('wrong',b.textContent===value&&!correct);
 });$('#nextButton').disabled=false;$('#hintButton').disabled=true;visible($('#retryButton'),!correct);setHeight();}
function retryQuestion(){if(!game||game.paused||!currentQ())return;const q=currentQ();
 q.locked=false;$$('.choice').forEach(b=>{b.disabled=false;b.classList.remove('correct','wrong')});
 $('#answerFeedback').textContent='もういちど やってみよう';$('#hintButton').disabled=true;visible($('#retryButton'),false);
 $('#nextButton').disabled=true;startClock();storeGame();}
function nextQuestion(){if(!game||!currentQ()?.responses?.length)return;closeClock('next_question');const m=MENU_DATA[game.menu_id];
 if(game.index>=m.count-1){endGame('completed');return;}
 game.index++;storeGame();showQuestion();startClock();}
function endGame(status,quiet=false){if(!game||game.finished)return;closeClock(status);const m=MENU_DATA[game.menu_id];
 game.finished=true;game.finished_at=nowISO();const first=game.questions.map(q=>q.responses[0]).filter(Boolean);
 game.score=first.filter(x=>x.correct).length;
 logEvent('session_ended',{status,finished_at:game.finished_at,questions_answered:first.length,total_questions:m.count,first_correct_count:game.score,
 unknown_or_unmeasured_ms:null,assignment_id:game.assignment_id});
 storeGame();releaseLease();
 if(status==='completed'){
   history.push({attempt_id:game.attempt_id,menu_id:game.menu_id,first_correct:game.score,total:m.count,
    finished_at:game.finished_at,linked_profile_id:game.profile_id||null});
   if(history.length>150)history=history.slice(-150);
   safePut(HIST_KEY,history);if(!quiet)showResults();
 }else{diag('challenge_stopped',{menu_id:game.menu_id,reason:status});if(!quiet){setView('normal');setTab('challenge')}}
 trySync();}
function gradeFor(score,total){const x=score/total;return x===1?'gold':x>=.8?'silver':x>=.6?'bronze':'white'}
function showResults(){if(!game)return;const m=MENU_DATA[game.menu_id];const grade=gradeFor(game.score,m.count),medal=$('#resultMedal');
 medal.className='medal '+grade;$('#resultGrade').textContent={gold:'きん',silver:'ぎん',bronze:'どう',white:'しろ'}[grade];
 $('#resultChar').textContent=m.subject==='letters'?'字':'語';$('#resultReading').textContent=m.subject==='letters'?'ジ':'ゴ';
 $('#resultScore').textContent=m.count+'もん ちゅう '+game.score+'もん せいかい';
 $('#resultStatus').textContent=game.profile_id?'ちょこまなへの学習記録を保存中':'未連携：この端末に記録しました。ちょこまなのメダルは未送信です。';
 const confirmed=deliveredStamps[game.attempt_id];if(confirmed)$('#resultStatus').textContent='ちょこまなにメダルを保存しました';
 setView('result');setHeight();trySync();}
function say(text){if(!appSettings.sound||!window.speechSynthesis)return false;
 try{speechSynthesis.cancel();const s=new SpeechSynthesisUtterance(text);s.lang='ja-JP';s.rate=.86;s.volume=appSettings.volume/100;
 speechSynthesis.speak(s);return true;}catch(e){diag('speech_error',{code:'speech_unavailable'});return false}}
function hint(){if(!game||game.paused||currentQ()?.responses.length)return;const q=currentQ();if(!q)return;
 if(say(q.prompt)){q.hint++;logEvent('support_used',{support_type:'hint',hint_kind:'tts_prompt',question_id:q.question_id},q);
 $('#hintState').textContent='もういちど きいて かんがえよう';storeGame();}else $('#hintState').textContent='よみあげが つかえません';}
function pauseToggle(){if(!game||game.finished)return;
 if(!game.paused){closeClock('explicit_break');game.paused=true;logEvent('session_paused',{reason:'explicit_break'});
 $('#pauseButton').textContent='さいかい';$('#hintState').textContent='きゅうけい してね';}
 else {game.paused=false;game.segment_id=isUUID();game.sequence=0;logEvent('session_resumed',{reason:'explicit_break'});
 $('#pauseButton').textContent='きゅうけい';$('#hintState').textContent='';startClock()};storeGame();}
function resumeRestoredGame(){if(!game||game.finished)return;game.segment_id=isUUID();game.sequence=0;
 if(currentQ())currentQ().restored=true;
 logEvent('session_resumed',{reason:'page_reload'});setView('game');showQuestion();startClock();}
function saveSettings(){safePut(SETTINGS_KEY,appSettings)}
function setSegment(root,attr,value){for(const b of root.querySelectorAll('button'))b.classList.toggle('selected',b.dataset[attr]===value)}
function rawWord(){return $('#wordText').value.trim()}
function updatePreview(){const s=rawWord();const status=$('#paperInfo');let word='';
 if(/[\u3400-\u9fff\uf900-\ufaff]/.test(s)){
  status.textContent='かんじを ひらがな・カタカナに なおしています…';
  enqueue('normalize',{text:s,script,word_mode:wordMode},true);return;
 }
 try{word=normalize(s,script)}catch(e){status.textContent=e.message;return}
 if(wordMode==='playful')status.textContent='おもしろい ことば：おんの ちがいを なおさず プレビューします。';
 else if(wordMode==='auto')status.textContent='ことばを かくにんしてね。はんだんできない ことばは かってに なおしません。';
 else status.textContent='ひらがな・カタカナの ひょうきだけ そろえました。';
 $('#wordText').value=word;lastWord=word;
 renderPaperPreview(word,orientation);visible($('#previewSection'),true);$('#pdfStatus').textContent='';setHeight();}
function normalize(value,kind){let s=String(value||'').normalize('NFKC').replace(/[。．、，!！?？\"'「」『』（）()\[\]【】\s]/g,'');
 if(kind==='hiragana')s=Array.from(s).map(x=>x>='ァ'&&x<='ヶ'?String.fromCharCode(x.charCodeAt(0)-96):x).join('');
 else s=Array.from(s).map(x=>x>='ぁ'&&x<='ゖ'?String.fromCharCode(x.charCodeAt(0)+96):x).join('');
 if(s.length<1||s.length>60)throw new Error('1〜60もじの ことばを いれてね');
 if(!(kind==='hiragana'?/^[ぁ-ゖー]+$/:/^[ァ-ヶー]+$/).test(s))throw new Error('かんじ・すうじが あるときは もじを なおしてね');
 return s;
}
function renderPaperPreview(word,dir){const out=$('#previewPaper');out.textContent='';out.className='preview '+dir;
 const len=Array.from(word).length,count=len<=3?3:len<=9?2:1,chunklen=dir==='horizontal'?8:11;
 const chunks=[];const chars=Array.from(word);for(let i=0;i<chars.length;i+=chunklen)chunks.push(chars.slice(i,i+chunklen));
 for(let j=0;j<count;j++)for(const ch of chunks){const group=document.createElement('div');group.className='trace-group';
  for(const kind of ['trace','blank']){const cells=document.createElement('div');cells.className='cells';
   ch.forEach(g=>{const e=document.createElement('div');e.className='cell';const x=document.createElement('span');x.textContent=kind==='trace'?g:'';e.appendChild(x);cells.appendChild(e)});group.appendChild(cells);
  }out.appendChild(group);}
}
function prepareMic(){const SR=window.SpeechRecognition||window.webkitSpeechRecognition;if(!SR){$('#voiceState').textContent='このブラウザは おんせいに たいおうしていません';return}
 recognition=new SR();recognition.lang='ja-JP';recognition.interimResults=false;recognition.maxAlternatives=1;
 recognition.onstart=()=>{$('#voiceState').textContent='きいています…';diag('microphone_started')};
 recognition.onresult=e=>{const transcript=e.results?.[0]?.[0]?.transcript||'';$('#wordText').value=transcript;
 $('#voiceState').textContent='このことばで いい？ を おしてね';diag('microphone_result',{characters:transcript.length});updatePreview();};
 recognition.onerror=e=>{$('#voiceState').textContent='マイクが つかえません（'+(e.error||'error')+'）';diag('microphone_error',{code:String(e.error||'unknown')})};
 recognition.onend=()=>diag('microphone_ended');}
function requestPDF(){if(/[\u3400-\u9fff\uf900-\ufaff]/.test(rawWord())){
  $('#pdfStatus').textContent='いんさつ まえに ことばを かくにんしてね';updatePreview();return;}
 let word;try{word=normalize(rawWord(),script)}catch(e){$('#pdfStatus').textContent=e.message;return}
 if(word!==lastWord){updatePreview();$('#pdfStatus').textContent='ことばを なおしたので、いんさつ まえに もういちど かくにんしてね。';return;}
 $('#pdfStatus').textContent='PDFを つくっています…';
 pdfPending=isUUID();enqueue('pdf',{text:word,script,orientation},true);}
function setPdfResult(result){if(!result.ok){$('#pdfStatus').textContent='PDFを つくれませんでした：'+(result.code||'error');diag('pdf_failed',{code:result.code});return}
 const bin=atob(result.pdf_base64),bytes=new Uint8Array(bin.length);for(let i=0;i<bin.length;i++)bytes[i]=bin.charCodeAt(i);
 const safe=lastWord.replace(/[^ぁ-ゖァ-ヿー\w]/g,'').slice(0,24)||'renshuu';exportBlob('こうひつ_'+safe+'.pdf',bytes,'application/pdf');
 $('#pdfStatus').textContent='PDFを つくりました。A4で いんさつしてね。';diag('pdf_created',{bytes:result.bytes,pdf_server_ms:result.pdf_ms});}
function showConnection(){const linked=!!currentProfile;const status=linked?'接続済み':'未連携';
 const pending=eventDB.pending.filter(e=>e.profile_id===currentProfile).length;
 $('#connectionState').textContent='ちょこまな：'+status+(linked?'（未送信 '+pending+' 件）':'（端末内に記録します）');
 $('#settingsConnection').textContent='ちょこまな：'+status+'　'+(linked?'未送信 '+pending+' 件':'この端末の記録は共有DBに送信しません');
 $('#metricPending').textContent=eventDB.pending.length;$('#metricSessions').textContent=history.length;
 $('#metricErrors').textContent=diagnostics.filter(r=>/error|failed/.test(r.type)).length;
}
function computeTimings(){const entries=diagnostics.filter(d=>d.type==='challenge_first_problem_painted');const arr=entries.map(e=>e.input_to_problem_ready_ms).filter(Number.isFinite).sort((a,b)=>a-b);
 const med=arr.length?arr[Math.floor(arr.length/2)]:null,p95=arr.length?arr[Math.ceil(arr.length*.95)-1]:null;
 const slow=diagnostics.filter(e=>e.type==='long_task');
 $('#timingSummary').textContent='チャレンジ開始→問題表示：'+(arr.length?'中央値 '+med+' ms / P95 '+p95+' ms / '+arr.length+'回':'未測定')+'\n'+
  '直近起動：'+(diagnostics.filter(d=>d.type==='startup_js').at(-1)?.iframe_js_ready_ms??'未測定')+' ms（iframe 内）\n'+
  '長時間タスク：'+slow.length+'件\n外部アプリのタップ→起動：計測不可（ちょこまな側の開始印が必要）';}
function downloadLearning(){const payload={specification_id:'chokomana.learning.v1',source_app:'kana',content_version:CONTENT_VERSION,
 exported_at:nowISO(),active_attempt:game,completion_cache:history,events_pending:eventDB.pending,events_unlinked:eventDB.local,
 acked_event_ids:eventDB.acked,grade_rule:'gold 100%, silver >=80%, bronze >=60%, white below',note:'紙の筆記は自動採点していない'};
 exportBlob('kana_learning_'+new Date().toISOString().slice(0,10)+'.json',JSON.stringify(payload,null,2),'application/json');}
function downloadDiag(format){const data={schema_version:1,app_version:APP_VERSION,exported_at:nowISO(),environment:{user_agent:navigator.userAgent,viewport:[innerWidth,innerHeight],language:navigator.language},
 notes:['All duration fields use milliseconds.','External app tap cannot be observed from this cross-origin iframe.','No word transcripts, answers, device bearer or service credentials in diagnostics.'],entries:diagnostics};
 if(format==='json')exportBlob('kana_diagnostics.json',JSON.stringify(data,null,2),'application/json');
 else{const keys=['at','type','duration_ms','input_to_problem_ready_ms','pointer_to_problem_ready_ms','menu_id','code'];const quote=v=>'"'+String(v??'').replace(/"/g,'""')+'"';
 exportBlob('kana_diagnostics.csv','\uFEFF'+[keys.join(','),...diagnostics.map(d=>keys.map(k=>quote(d[k])).join(','))].join('\r\n'),'text/csv;charset=utf-8')}
}
function enqueue(kind,fields={},priority=false){const item={delivery_id:isUUID(),kind,device_token:deviceToken,...fields};if(priority)requestQueue.unshift(item);else requestQueue.push(item);pump();}
function pump(){if(requestBusy||!requestQueue.length||!window.Streamlit?.setComponentValue)return;requestBusy=requestQueue.shift();
 diag('bridge_request',{kind:requestBusy.kind,count:requestBusy.events?.length||null});try{window.Streamlit.setComponentValue(requestBusy)}catch(e){diag('bridge_error',{code:'send_failed'});requestBusy=null;}}
function handleBridge(result){if(!result||result.delivery_id===lastResultDelivery)return;
 if(!requestBusy||result.delivery_id!==requestBusy.delivery_id)return;
 lastResultDelivery=result.delivery_id;const currentInFlight=requestBusy,kind=requestBusy.kind;requestBusy=null;
 diag('bridge_result',{kind,ok:result.ok===true,code:result.code,acked:result.acked?.length||0});
 if(kind==='identity'){
  if(result.ok&&result.profile_id){currentProfile=result.profile_id;identityStatus='linked';diag('identity_linked');
    if(result.assignment?.menu_id&&MENU_DATA[result.assignment.menu_id]){window.__kanaAssignment=result.assignment;
     const planKey=result.assignment.plan_item_id||result.assignment.menu_id;
     if((!game||game.finished)&&!launchedPlanIds.has(planKey)){launchedPlanIds.add(planKey);beginGame(result.assignment.menu_id,'verified_plan_ticket');}}}
  else identityStatus='unlinked';
  showConnection();if(currentProfile)trySync();
 }
 if(kind==='pdf')setPdfResult(result);
 if(kind==='normalize'){
   if(result.ok && result.normalized?.text && currentInFlight?.text===$('#wordText').value){
     $('#wordText').value=result.normalized.text;lastWord=result.normalized.text;
     renderPaperPreview(lastWord,orientation);visible($('#previewSection'),true);
     $('#paperInfo').textContent=result.normalized.message;
   }else if(!result.ok)$('#paperInfo').textContent='よみのへんかんに しっぱいしました。もじを なおしてね。';
 }
 if(kind==='sync'){
  if(result.ok&&Array.isArray(result.acked)){
   const ack=new Set(result.acked);eventDB.pending=eventDB.pending.filter(e=>!ack.has(e.event_id));
   eventDB.acked.push(...result.acked);if(eventDB.acked.length>2000)eventDB.acked=eventDB.acked.slice(-2000);
   safePut(EVENTS_KEY,eventDB);showConnection();
  }else{identityStatus=result.code==='not_linked'?'unlinked':identityStatus;diag('sync_failed',{code:result.code});}
 }
 if(kind==='stamp'){
  const aid=result.stamp?.attempt_id;
  if(result.ok&&aid){deliveredStamps[aid]=result.stamp;safePut('chokomana_kana_stamp_ack_v1',deliveredStamps);
    if(game?.attempt_id===aid&&currentView==='result')$('#resultStatus').textContent='ちょこまなにメダルを保存しました';}
  else diag('stamp_failed',{code:result.code});
 }
 pump();if(!requestBusy&&currentProfile&&kind!=='pdf'&&result.ok&&kind!=='identity')setTimeout(trySync,200);
}
function trySync(){if(!currentProfile||requestBusy||requestQueue.some(e=>e.kind==='sync'))return;
 const pending=eventDB.pending.filter(e=>e.profile_id===currentProfile).slice(0,30);
 if(pending.length){enqueue('sync',{events:pending});return;}
 const needs=history.find(h=>h.linked_profile_id===currentProfile&&!deliveredStamps[h.attempt_id]);
 if(needs && !requestQueue.some(e=>e.kind==='stamp'))enqueue('stamp',{attempt_id:needs.attempt_id});
}
function onRender(args){const result=args?.result;
 if(result){handleBridge(result)}
 if(args?.chokomana_home_url){const safe=String(args.chokomana_home_url);if(safe.startsWith('https://')){for(const el of [$('#hubTop'),...$$('.hubInGame'),$('#hubResult')])el.href=safe;}}
 if(args?.server_startup_ms&&!window.__kanaServerTimingSeen){window.__kanaServerTimingSeen=true;diag('server_boot_timing',{python_to_component_ms:args.server_startup_ms})}
 setHeight();}
function setup(){setNavLinks();renderMenus();
 if(typeof ResizeObserver!=='undefined')new ResizeObserver(setHeight).observe($('#app'));
 if(typeof MutationObserver!=='undefined')new MutationObserver(setHeight).observe($('#app'),{attributes:true,childList:true,subtree:true,attributeFilter:['class','hidden']});
 $$('[data-tab]').forEach(b=>b.addEventListener('click',()=>{if(currentView!=='normal')setView('normal');setTab(b.dataset.tab);if(b.dataset.tab==='settings'){showConnection();computeTimings()}}));
 $$('.backHome').forEach(b=>b.addEventListener('click',()=>{if(game&&!game.finished)endGame('interrupted',true);setView('normal');setTab('challenge')}));
 $('#hintButton').addEventListener('click',hint);$('#retryButton').addEventListener('click',retryQuestion);$('#nextButton').addEventListener('click',nextQuestion);
 $('#pauseButton').addEventListener('click',pauseToggle);$('#abandonButton').addEventListener('click',()=>endGame('interrupted'));
 $('#againButton').addEventListener('click',()=>beginGame(game.menu_id,'retry_button'));
 $$('[data-script]').forEach(b=>b.addEventListener('click',()=>{script=b.dataset.script;setSegment($('#scriptSegment'),'script',script);visible($('#previewSection'),false)}));
 $$('[data-word-mode]').forEach(b=>b.addEventListener('click',()=>{wordMode=b.dataset.wordMode;setSegment($('#wordMode'),'wordMode',wordMode)}));
 $$('[data-orientation]').forEach(b=>b.addEventListener('click',()=>{orientation=b.dataset.orientation;setSegment($('#directionSegment'),'orientation',orientation);visible($('#previewSection'),false)}));
 $('#previewBtn').addEventListener('click',updatePreview);$('#editPdf').addEventListener('click',()=>{$('#wordText').focus();visible($('#previewSection'),false)});
 $('#downloadPdf').addEventListener('click',requestPDF);$('#voiceBtn').addEventListener('click',()=>{if(!recognition)prepareMic();if(!recognition)return;try{recognition.start()}catch(e){$('#voiceState').textContent='マイクの きどうに しっぱいしました';diag('microphone_error',{code:'start_failed'})}});
 $$('[data-sound]').forEach(b=>b.addEventListener('click',()=>{appSettings.sound=b.dataset.sound==='on';saveSettings();setSegment($('#soundSwitch'),'sound',appSettings.sound?'on':'off')}));
 $('#volumeSlider').addEventListener('input',e=>{appSettings.volume=Number(e.target.value);$('#volumeOutput').textContent=appSettings.volume+'%';saveSettings()});
 $('#volumeSlider').value=String(appSettings.volume);$('#volumeOutput').textContent=appSettings.volume+'%';setSegment($('#soundSwitch'),'sound',appSettings.sound?'on':'off');
 $('#exportLearning').addEventListener('click',downloadLearning);$('#syncNow').addEventListener('click',()=>{enqueue('identity');setTimeout(trySync,700)});
 $('#exportDiagnosticsJson').addEventListener('click',()=>downloadDiag('json'));$('#exportDiagnosticsCsv').addEventListener('click',()=>downloadDiag('csv'));
 $('#clearDiagnostics').addEventListener('click',()=>{diagnostics=[];safePut(performanceLogKey,diagnostics);computeTimings()});
 window.addEventListener('error',e=>diag('js_error',{code:'script_error',message:String(e.message||'').slice(0,150)}));
 window.addEventListener('unhandledrejection',e=>diag('promise_error',{code:'promise_rejected'}));
 document.addEventListener('visibilitychange',()=>{if(document.hidden){closeClock('hidden');if(game&&!game.finished)logEvent('session_paused',{reason:'hidden'})}
  else if(game&&!game.finished&&!game.paused&&currentView==='game'){logEvent('session_resumed',{reason:'visible'});startClock()}});
 window.addEventListener('pagehide',()=>{closeClock('pagehide');releaseLease();safePut(EVENTS_KEY,eventDB)});
 if(window.PerformanceObserver){try{new PerformanceObserver(list=>{for(const e of list.getEntries())if(e.duration>100)diag('long_task',{duration_ms:Math.round(e.duration),start_ms:Math.round(e.startTime)})}).observe({type:'longtask',buffered:true})}catch(e){}}
 window.setInterval(()=>{if(game&&!game.finished&&currentView==='game'&&!game.paused&&!document.hidden){checkpointClock('periodic_checkpoint')}},5000);
 window.setInterval(()=>{if(currentProfile)trySync()},30000);
 if(game&&!game.finished){resumeRestoredGame()}else if(game&&game.finished){/* default to menu; result is available through local record */}
 showConnection();setHeight();
 // The initial home and every mode button are interactive before server identity/network I/O.
 setTimeout(()=>enqueue('identity'),180);
}
if(window.Streamlit){window.Streamlit.events?.addEventListener?.(window.Streamlit.RENDER_EVENT,e=>onRender(e.detail?.args||{}));window.Streamlit.setComponentReady?.()}else{window.__kanaDevMode=true}
setup();window.__kanaTest={beginGame,answer,nextQuestion,gradeFor,normalize,updatePreview,prepareQuestions,getGame:()=>game, getEvents:()=>eventDB,getDiagnostics:()=>diagnostics,renderPaperPreview,closeClock};
</script></body></html>'''


def frontend_html() -> str:
    menus={m["menu_id"]:{
       "menu_id":m["menu_id"],"script":m["script"],"subject":m["subject"],
       "level":m["level"],"label":m["label"],"count":m["question_count_default"],
       "skill_ids":m["skill_ids"],"group":m["progression_group_id"]} for m in MENUS}
    jsbank=[{k:q[k] for k in ("question_id","menu_id","prompt","answer")} for q in QUESTION_BANK.values()]
    # JSON cannot end the surrounding script tag even if content is extended later.
    def js(v):return json.dumps(v,ensure_ascii=False,separators=(",",":")).replace("</","<\\/").replace("\u2028","\\u2028")
    text=HTML_TEMPLATE
    for key,value in {
       "{{APP_VERSION}}":APP_VERSION,"{{MENU_JSON}}":js(menus),"{{BANK_JSON}}":js(jsbank),
       "{{VERSION_JSON}}":js(APP_VERSION),"{{CONTENT_JSON}}":js(CONTENT_VERSION),
       "{{HUB_URL_JSON}}":js(DEFAULT_HUB_URL)
    }.items():text=text.replace(key,value)
    return text


def _smoke_tests():
    assert len(MENUS)==8
    assert len(QUESTION_BANK)>=180
    assert len({q["question_id"] for q in QUESTION_BANK.values()})==len(QUESTION_BANK)
    assert all(sum(q["menu_id"]==m["menu_id"] for q in QUESTION_BANK.values())>=m["question_count_default"] for m in MENUS)
    assert to_katakana("ひらがな") == "ヒラガナ"
    assert to_hiragana("カタカナ") == "かたかな"
    assert normalize_worksheet_text("　ヒラガナ！　","hiragana")=="ひらがな"
    try:normalize_worksheet_text("漢字","hiragana")
    except ValueError:pass
    else:raise AssertionError("kanji should not bypass preview transcription")
    assert worksheet_plan("あいう","horizontal")["repeats"]==3
    assert worksheet_plan("あいうえ","vertical")["repeats"]==2
    assert worksheet_plan("あいうえおかきくけこ","horizontal")["repeats"]==1
    rows=catalog_rows("https://example.streamlit.app/")
    assert len([r for r in rows if r["entry_type"]=="menu"])==8
    assert all(r["payload"].get("supports_direct_launch") is True for r in rows if r["entry_type"]=="menu")
    pdf1=generate_worksheet_pdf("あいう","hiragana","horizontal")
    pdf2=generate_worksheet_pdf("アイウエオ","katakana","vertical")
    assert pdf1.startswith(b"%PDF-") and pdf2.startswith(b"%PDF-")
    # The public learning catalog excludes question rows, which include answers.
    assert all(r["entry_type"]!="question" for r in rows)
    fake_uuid=str(uuid.uuid4());q=next(iter(QUESTION_BANK.values()))
    row={"schema_version":1,"event_id":str(uuid.uuid4()),"profile_id":fake_uuid,"source_app":SOURCE_APP,
      "app_version":APP_VERSION,"content_version":CONTENT_VERSION,"attempt_id":str(uuid.uuid4()),
      "occurrence_id":str(uuid.uuid4()),"question_id":q["question_id"],"segment_id":str(uuid.uuid4()),
      "sequence":1,"occurred_at":_now(),"event_type":"response_submitted","payload":{
        "response_id":str(uuid.uuid4()),"response_index":1,"response_status":"answered", "input_method":"choice",
        "speech_status":"not_used","assessment_method":"automatic","response_value":q["answer"],
        "is_correct":True,"timing_quality":"complete"}}
    assert validate_events([row],fake_uuid)[0]["event_hash"]
    row["payload"]["is_correct"]=False
    try:validate_events([row],fake_uuid)
    except SyncError:pass
    else:raise AssertionError("Incorrect claimed grade was accepted")
    assert "chokomana_append_kana_events_v1" in SQL
    assert "send" not in frontend_html()[:80]  # Ensure template resolved
    print("PASS: menus, bank, conversion, printable PDF, event validation, SQL, frontend")


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
    </style>""",unsafe_allow_html=True)
    if "_kana_ticket" not in st.session_state:
        t=st.query_params.get("cm_launch")
        if isinstance(t,str) and re.fullmatch(r"[A-Za-z0-9_-]{32,256}",t):st.session_state["_kana_ticket"]=t
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
    if len(sys.argv)>=2 and sys.argv[1]=="--chokomana-sql":print(SQL)
    elif len(sys.argv)>=2 and sys.argv[1]=="--chokomana-catalog":print(json.dumps(catalog_rows(),ensure_ascii=False,indent=2))
    elif len(sys.argv)>=2 and sys.argv[1]=="--self-test":_smoke_tests()
    elif len(sys.argv)>=3 and sys.argv[1]=="--sample-pdf":
        p=Path(sys.argv[2]);p.write_bytes(generate_worksheet_pdf("りんご","hiragana","horizontal"));print(p)
    else:_run_streamlit()
